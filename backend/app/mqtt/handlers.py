"""MQTT 消息处理器：把上行消息转成业务操作。

这是 MQTT 层与 service 层的粘合层。
每个 handler 自己开数据库会话（MQTT 循环不持有请求级会话）。
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from typing import Any

from loguru import logger

from app.db.partitions import ensure_track_partitions_around
from app.db.session import get_session_factory
from app.models.event import EventStatus, WasteClass
from app.models.task import Task
from app.schemas import EventIngest, GeoPoint, EventAggregate, DetectionItem
from app.services.dispatch import DispatchEngine, finalize_dispatch
from app.services.event import EventService
from app.ws.manager import ws_manager


async def handle_event(topic: str, payload: dict[str, Any]) -> None:
    """处理垃圾检测事件上报。

    主题：marine/{site_id}/{device_id}/event
    """
    try:
        ingest = EventIngest(
            event_id=payload["event_id"],
            device_id=payload["device_id"],
            device_type=payload.get("device_type", "shore_camera"),
            timestamp=_parse_ts(payload.get("timestamp")),
            location=GeoPoint(
                lng=payload["location"]["lng"],
                lat=payload["location"]["lat"],
            ),
            detections=[
                DetectionItem(
                    **{"class": d["class"], "confidence": d["confidence"], "bbox": d["bbox"]}
                )
                for d in payload.get("detections", [])
            ],
            aggregate=EventAggregate(**payload["aggregate"]),
            evidence_url=payload.get("evidence_url"),
            model_version=payload.get("model_version"),
            seq=payload["seq"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(f"[MQTT] 事件报文格式错误：{exc} | {payload}")
        return

    async with get_session_factory()() as session:
        service = EventService(session)
        # MQTT 入口同样在下方同步派单，不投 Redis Stream，避免双派。
        result = await service.ingest(ingest, enqueue_dispatch=False)
        await session.commit()

        if result.duplicate:
            return

        # 推送大屏告警
        await ws_manager.push_new_event(
            {
                "event_id": ingest.event_id,
                "device_id": ingest.device_id,
                "main_class": ingest.aggregate.main_class,
                "lng": ingest.location.lng,
                "lat": ingest.location.lat,
                "det_count": ingest.aggregate.count,
                "confidence": ingest.aggregate.max_confidence,
                "event_time": ingest.timestamp.isoformat(),
            }
        )

        # 告警外发到企业微信（仅高优先级类别）。
        # ★ 用 create_task 异步旁路，绝不 await：外发失败/超时不能阻塞派单主流程。
        if ingest.aggregate.main_class in WasteClass.HIGH_PRIORITY:
            from app.services.notify import build_alert_text, send_wecom_alert

            alert = build_alert_text(
                WasteClass.LABELS.get(ingest.aggregate.main_class, ingest.aggregate.main_class),
                ingest.device_id,
                ingest.location.lng,
                ingest.location.lat,
                ingest.timestamp.isoformat(),
            )
            asyncio.create_task(send_wecom_alert(alert))

        # 自动派单（仅高优先级类别）
        # ★ 白名单必须取自 WasteClass.HIGH_PRIORITY，不能在此硬编码字面量：
        #   api/v1/events.py 的 HTTP 入库路径用的是同一个常量。两处各写一份
        #   必然漂移 —— 改了策略却只改一处，MQTT 与 HTTP 两条入口行为就不一致。
        if ingest.aggregate.main_class in WasteClass.HIGH_PRIORITY:
            await _try_dispatch(ingest.event_id)


async def _try_dispatch(event_id: str) -> Task | None:
    """尝试为事件派单，成功则下发 MQTT 并推送看板。

    ★ 返回本次**实际创建/命中的** Task（未派成功返回 None）。

    为什么必须回传而不是让调用方自己去查：
        调用方（HTTP 入口）需要把 task_id 写进上报响应体，
        若让它「查全库最新一条任务」，在多事件并发时必然张冠李戴 ——
        而且库里只要有任何一条历史任务，就会误报派单成功。
        task_id 是**本次调用的产物**，只能由本函数回传，不能事后反查。
    """
    from app.core.exceptions import NoRobotAvailableError
    from app.mqtt.client import mqtt_client

    async with get_session_factory()() as session:
        engine = DispatchEngine(session)
        from app.repositories import EventRepository

        event = await EventRepository(session).get_by_event_id(event_id)
        if event is None or event.status != EventStatus.NEW:
            return None

        try:
            task = await engine.dispatch_for_event(event)
        except NoRobotAvailableError as exc:
            logger.warning(f"[MQTT] 派单失败（{exc}），事件等待补派：{event_id}")
            return None

        if task is None:
            return None

        await session.commit()

        # ★ 下发指令 + 推送看板走统一收尾，不在这里手工拼负载。
        #   四个派单入口曾只有两个做了收尾，详见 finalize_dispatch 的说明。
        await finalize_dispatch(task)

        return task


async def handle_telemetry(
    topic: str,
    payload: dict[str, Any],
    *,
    session_factory: Any | None = None,
) -> None:
    """处理设备遥测（心跳、电量、仓容）。

    主题：marine/{site_id}/{device_id}/telemetry
    QoS0 —— 丢一帧无所谓，但要更新在线状态。
    """
    device_id = payload.get("device_id") or _device_from_topic(topic)
    if not device_id:
        return

    if session_factory is None:
        session_factory = get_session_factory

    loc = payload.get("location")
    if not isinstance(loc, dict):
        loc = {}
    bins = payload.get("bins")
    if not isinstance(bins, dict):
        bins = {}

    async with session_factory()() as session:
        from app.repositories import DeviceRepository

        repo = DeviceRepository(session)
        await repo.upsert_heartbeat(device_id, status="online")

        # 机器人遥测额外记录轨迹与仓容
        if payload.get("device_type") == "robot" or device_id.startswith("RBT"):
            from app.models.misc import Track

            lng, lat = _valid_lnglat(loc)

            # 更新设备 meta 中的电量与仓容（供派单引擎读取）
            device = await repo.get_by_device_id(device_id)
            if device is not None:
                meta = dict(device.meta or {})
                meta["battery"] = payload.get("battery", meta.get("battery"))
                if bins:
                    meta["bins"] = bins
                device.meta = meta

                # 无有效坐标时不得写 POINT(0 0)：那会在几内亚湾生成
                # 一条看似合法、实际完全错误的机器人轨迹。
                if lng is not None and lat is not None:
                    # ★ 先保证 t_track 当天分区存在，再插轨迹。
                    #   t_track 是按日分区表，缺分区时 Postgres 会直接拒绝 INSERT
                    #   （CheckViolationError: no partition of relation "t_track" found），
                    #   而分区只在数据库首次初始化时建了一批、之后没人补 ——
                    #   一旦日期滚出窗口，所有机器人轨迹都写不进去，
                    #   表现是「新建工单点仿真 → 红色异常」。
                    #   放在这里自愈而不是依赖 cron：写入路径自己保证前置条件。
                    await ensure_track_partitions_around(session)

                    device.location = f"SRID=4326;POINT({lng} {lat})"
                    session.add(
                        Track(
                            robot_id=device_id,
                            task_id=payload.get("task_id"),
                            location=f"SRID=4326;POINT({lng} {lat})",
                            battery=_safe_int(payload.get("battery")),
                            bin_foam=_safe_float(bins.get("foam")),
                            bin_plastic=_safe_float(bins.get("plastic")),
                            bin_mixed=_safe_float(bins.get("mixed")),
                            speed=_safe_float(payload.get("speed")),
                        )
                    )

        await session.commit()

    await ws_manager.push_robot_status(
        {
            "robot_id": device_id,
            "battery": payload.get("battery"),
            "status": payload.get("status", "idle"),
            "lng": loc.get("lng"),
            "lat": loc.get("lat"),
            "task_id": payload.get("task_id"),
            "bins": bins,
            "heading": payload.get("heading"),
            "speed": payload.get("speed"),
        }
    )


async def handle_device_status(topic: str, payload: dict[str, Any]) -> None:
    """处理设备上下线状态（LWT 遗嘱消息）。

    主题：marine/{site_id}/{device_id}/status
    """
    device_id = payload.get("device_id") or _device_from_topic(topic)
    if not device_id:
        return

    online = bool(payload.get("online", False))
    status = "online" if online else "offline"

    async with get_session_factory()() as session:
        from app.repositories import DeviceRepository

        await DeviceRepository(session).upsert_heartbeat(device_id, status=status)
        await session.commit()

    logger.info(f"[MQTT] 设备 {device_id} 状态：{status}")


# 机器人上报状态 → 任务状态 的映射。
#
# ★ 这张表必须覆盖 mqtt-topics.md §5.6 声明的**全部**取值。
#   曾经漏了 `done`，后果不是报错，而是整条闭环断在最后一步：
#   机器人干完活上报 done → 平台不认识这个值 → 静默忽略 →
#   任务永远停在 collecting、事件永远不 resolved。
#   大屏上表现为「工单卡在作业中，永远不完成」，
#   而这期间**没有任何日志**（mapping.get 未命中返回 None，被 if 吞掉）。
#
#   `returning` 映射到 collecting 是有意的：返航仍属作业中，
#   状态机里也没有单独的「返航」态。
ROBOT_STATUS_MAP: dict[str, str] = {
    "navigating": "navigating",
    "collecting": "collecting",
    "returning": "collecting",
    "done": "done",
}


async def handle_robot_progress(
    topic: str,
    payload: dict[str, Any],
    *,
    session_factory: Any | None = None,
) -> None:
    """处理机器人作业进度回传。

    主题：robot/{robot_id}/task/progress

    契约见 `docs/mqtt-topics.md` §5.6。四种 status 的语义：

    | 上报 status   | 任务状态迁移               | 附带字段 |
    | --- | --- | --- |
    | `navigating`  | assigned → navigating      | — |
    | `collecting`  | navigating → collecting    | — |
    | `returning`   | 映射为 collecting（返航仍属作业中） | — |
    | `done`        | collecting → done          | `collected_weight` / `review_result` / `evidence_url` |

    `done` 由 `DispatchEngine.transition` 负责写 `finished_at`
    并把事件同步为 `resolved` —— 闭环在这一步合上。
    """
    robot_id = payload.get("robot_id") or _robot_from_topic(topic)
    task_id = payload.get("task_id")
    if not robot_id or not task_id:
        return

    robot_status = payload.get("status")
    target = ROBOT_STATUS_MAP.get(robot_status or "")
    if target is None:
        # 未识别的状态值要**留痕**。契约扩展时（机器人固件升级上报新状态）
        # 这条日志是唯一能立刻发现「平台不认识新值」的线索 ——
        # 否则会重演 done 那次：默默丢弃，直到有人盯着大屏问"为什么不完成"。
        logger.warning(
            f"[MQTT] 未知的作业状态 '{robot_status}'（task={task_id}），已忽略。"
            f" 已知取值：{sorted(ROBOT_STATUS_MAP)}"
        )
        return

    if session_factory is None:
        session_factory = get_session_factory

    async with session_factory()() as session:
        from app.repositories import TaskRepository

        task = await TaskRepository(session).get_by_task_id(task_id)
        if task is None:
            logger.warning(f"[MQTT] 作业回传找不到任务：{task_id}")
            return

        engine = DispatchEngine(session)
        changed = False

        from app.models.task import TaskStatus

        if target != task.status:
            if TaskStatus.can_transition(task.status, target):
                # `done` 要把本次作业的产出一起落库，否则「清理了多少」
                # 永远没有数据来源，报表中心就是空的。
                await engine.transition(
                    task,
                    target,
                    collected_weight=(
                        payload.get("collected_weight") if target == TaskStatus.DONE else None
                    ),
                    review_result=(
                        payload.get("review_result") if target == TaskStatus.DONE else None
                    ),
                    remark=payload.get("evidence_url") if target == TaskStatus.DONE else None,
                )
                changed = True
            else:
                # 乱序到达（如 done 先于 collecting 抵达）不算错误，
                # 但必须记下来 —— 静默跳过会让排查者以为是消息丢了。
                logger.warning(
                    f"[MQTT] 任务 {task_id} 状态无法从 {task.status} 迁移到 {target}"
                    f"（机器人上报 {robot_status}），已跳过"
                )
        elif target == TaskStatus.DONE:
            # ★ 幂等：重复的 done 也要把产出写回。
            #   机器人可能因 QoS1 重传而多次上报同一个 done，
            #   第一次已完成状态迁移，后几次 target == task.status 会走进这里。
            #   只补写仍为空的字段，避免用重复上报覆盖已确认的数据。
            changed = _backfill_task_outcome(task, payload) or changed

        await session.commit()

        await ws_manager.push_task_update(
            {
                "task_id": task.task_id,
                # ★ event_id 不能省：api.md §WS 消息表声明 task_update 的 data
                #   含 event_id/robot_id/status/task_id 四个字段。
                #   这里曾漏掉 event_id —— 前端 pushTask 用 {...data} 展开，
                #   缺字段会静默变成 undefined，不报错。
                #   当前前端只把 taskFeed 当"有更新就重拉"的触发器，
                #   所以暂时看不出问题；但契约是四个字段就得给四个。
                "event_id": task.event_id,
                "robot_id": task.robot_id,
                "status": task.status,
                "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            }
        )

    if changed:
        logger.info(f"[MQTT] 任务 {task_id} 进度更新为 {target}（机器人 {robot_id}）")


def _backfill_task_outcome(task: Any, payload: dict[str, Any]) -> bool:
    """把作业产出回填到任务上，只填空值。返回是否有改动。

    为什么需要它：QoS1 是「至少一次」，机器人重传 done 时
    第一次已经做过状态迁移，后续几次 target 会等于当前状态而跳过迁移。
    若不在这里回填，重传的消息就等于白收 —— 而丢的恰恰可能是
    `collected_weight` 这类支撑报表的关键数字。
    """
    changed = False
    if task.collected_weight is None and payload.get("collected_weight") is not None:
        task.collected_weight = payload["collected_weight"]
        changed = True
    if task.review_result in (None, "pending") and payload.get("review_result"):
        task.review_result = payload["review_result"]
        changed = True
    if not task.finished_at:
        task.finished_at = datetime.now()
        changed = True
    return changed


async def handle_robot_ack(
    topic: str,
    payload: dict[str, Any],
    *,
    tracker: Any | None = None,
    session_factory: Any | None = None,
) -> str | None:
    """处理机器人任务确认（ACK），消费冻结回执信封（WP-14C）+ 落审计账本（WP-14D）。

    主题：robot/{robot_id}/cmd/ack

    ★ 修复的断链：旧实现只认顶层 ``task_id``，而冻结回执信封
    （docs/device-interface.md §5）没有顶层 ``task_id``，于是所有冻结
    ACK 都被静默丢弃，任务永远停在 assigned。

    冻结接口（WP-14C 条款 1~7，WP-14D 条款 2~3）：
    - 解析优先冻结信封（ack_id/command_id/device_id/seq/received_at/
      accepted/reason/mode），兼容旧字段（task_id/robot_id/accepted/ts）；
      字段缺失或类型错误只记 warning 并丢弃，绝不让 MQTT 主循环抛异常。
    - task_id 优先顶层兼容字段，否则仅接受 command_id=cmd_{task_id} 反推；
      两者不一致整条拒绝。
    - payload.device_id（旧 robot_id）、topic robot_id、task.robot_id
      三者必须一致，否则整条拒绝、不得推进（内存判重为 duplicate 时跳过
      身份校验 —— 首次拒绝回执已清空 robot_id，重复回执重走校验必然失配）。
    - 判定优先级 duplicate > late > out_of_order > new（AckTracker）。
    - **账本写入与状态推进同一事务**（WP-14D §3）：先校验与判定，再写
      t_task_ack 行，最后提交；写库异常整体回滚，不留下「已推进但无回执
      证据」的状态。账本以 ``command_id`` 为准：首次建规范行，重复只累计
      （含内存漏判的进程重启场景 —— record_arrival 返回 duplicate 时
      不推进状态）。
    - accepted=true：仅当任务仍为 assigned 时推进 navigating；
      其他状态不重复推进。
    - accepted=false：首次回执且任务仍 assigned 时，记录原因、清空
      robot_id 并 assigned -> pending，交给现有补派轮处理；本 handler
      **不递归重派**；重复拒绝不重复回退。

    ★ 账本能力探测：会话具备 ``execute``（真实 AsyncSession）才写账本；
    测试替身会话不具备时，显式回落 WP-14C 内存跟踪并记 warning ——
    不伪装持久化（与 ack.py 的回落策略一致）。生产路径必然走账本。

    返回判定结果（new/duplicate/late/out_of_order），未受理返回 None。
    依赖可注入（tracker / session_factory），测试不触真实 MQTT/DB。
    """
    try:
        from datetime import datetime, timezone as _tz

        from app.mqtt.ack import AckResult, AckEnvelope, parse_ack_envelope

        envelope: AckEnvelope | None = parse_ack_envelope(payload)
        if envelope is None:
            return None

        if tracker is None:
            from app.mqtt.client import mqtt_client

            tracker = mqtt_client.ack_tracker

        topic_robot = _robot_from_topic(topic)
        if topic_robot is None:
            logger.warning(f"[MQTT-ACK] 主题无法解析 robot_id：{topic}")
            return None

        if session_factory is None:
            from app.db.session import get_session_factory as _factory

            session_factory = _factory

        from app.models.task import TaskStatus

        # 先做幂等判定：重复回执直接返回首次规范回执，不再校验身份、
        # 不再推进状态 —— 只把重复到达累计进账本（QoS1 幂等，WP-14C 条款 5）。
        outcome = tracker.classify(envelope)
        if outcome == AckResult.DUPLICATE:
            first = tracker.ack_for(envelope.command_id)
            logger.info(
                f"[MQTT-ACK] 重复回执 {envelope.command_id}，返回首次规范回执"
                f"（首次判定={first.outcome if first else '?'}），不重复推进"
            )
            # 只累计账本重复次数（不覆盖首次规范回执）；会话不支持账本时
            # （测试替身）静默跳过 —— 判定与幂等已由内存 tracker 完成。
            async with session_factory()() as session:
                if callable(getattr(session, "execute", None)):
                    from app.repositories import TaskAckRepository

                    await TaskAckRepository(session).record_arrival(
                        command_id=envelope.command_id,
                        task_id=envelope.task_id,
                        device_id=envelope.device_id,
                        seq=envelope.seq,
                        outcome=outcome,
                        accepted=envelope.accepted,
                        reason=envelope.reason,
                        mode=envelope.mode,
                        received_at=datetime.fromtimestamp(
                            envelope.received_at, tz=_tz.utc
                        ),
                        raw_payload=payload,
                    )
                    await session.commit()
            return outcome

        # 非重复回执：同一事务内「校验 → 写账本 → 推进状态 → 提交」。
        async with session_factory()() as session:
            from app.repositories import TaskAckRepository, TaskRepository

            task = await TaskRepository(session).get_by_task_id(envelope.task_id)
            if task is None:
                logger.warning(
                    f"[MQTT-ACK] 找不到任务（回执已丢弃，不登记）："
                    f"task={envelope.task_id} command={envelope.command_id}"
                )
                return None

            if (
                envelope.device_id != topic_robot
                or topic_robot != task.robot_id
            ):
                logger.warning(
                    f"[MQTT-ACK] 设备身份不一致，整条拒绝（不登记不推进）："
                    f"payload.device_id={envelope.device_id} "
                    f"topic.robot_id={topic_robot} "
                    f"task.robot_id={task.robot_id} task={task.task_id}"
                )
                return None

            # 账本登记（与状态推进同一事务）：首次建规范行；已存在（内存
            # 漏判，如进程重启后同一 command_id 再达）→ 返回 duplicate，
            # 只累计、不推进 —— 状态推进绝不会发生在无回执证据之后。
            ledger_supported = callable(getattr(session, "execute", None))
            if ledger_supported:
                recorded = await TaskAckRepository(session).record_arrival(
                    command_id=envelope.command_id,
                    task_id=envelope.task_id,
                    device_id=envelope.device_id,
                    seq=envelope.seq,
                    outcome=outcome,
                    accepted=envelope.accepted,
                    reason=envelope.reason,
                    mode=envelope.mode,
                    received_at=datetime.fromtimestamp(
                        envelope.received_at, tz=_tz.utc
                    ),
                    raw_payload=payload,
                )
            else:
                # 测试替身 / 非 ORM 会话：显式回落 WP-14C 内存跟踪，
                # 不伪装持久化（生产 AsyncSession 必然走账本路径）。
                recorded = outcome
                logger.warning(
                    f"[MQTT-ACK] 会话不支持账本写入，本次仅内存跟踪："
                    f"command={envelope.command_id}"
                )

            if recorded == AckResult.DUPLICATE:
                # 账本已有该 command_id 的规范回执（内存漏判 / 并发冲突）
                # → 只累计重复次数，不推进状态。
                if ledger_supported:
                    await session.commit()
                logger.info(
                    f"[MQTT-ACK] 账本判定 {envelope.command_id} 为重复回执，"
                    f"仅累计不推进"
                )
                return AckResult.DUPLICATE

            engine = DispatchEngine(session)
            if envelope.accepted:
                # accepted=true：仅当任务仍为 assigned 时推进 navigating
                if task.status == TaskStatus.ASSIGNED:
                    await engine.transition(task, TaskStatus.NAVIGATING)
                    logger.info(
                        f"[MQTT-ACK] 任务 {task.task_id} 已确认（{recorded}），"
                        f"进入导航状态（机器人 {topic_robot}）"
                    )
                else:
                    logger.warning(
                        f"[MQTT-ACK] 任务 {task.task_id} 已不在 assigned"
                        f"（当前 {task.status}），不再重复推进（outcome={recorded}）"
                    )
            else:
                # accepted=false：首次回执时，任务仍 assigned → 记录原因并回退 pending
                if task.status == TaskStatus.ASSIGNED:
                    reason = envelope.reason or "device_rejected"
                    await engine.transition(
                        task, TaskStatus.PENDING, remark=f"设备拒绝：{reason}"
                    )
                    task.robot_id = None
                    logger.warning(
                        f"[MQTT-ACK] 任务 {task.task_id} 被设备拒绝"
                        f"（{recorded}，reason={reason}），回退待派单，"
                        f"交由现有补派轮处理（本 handler 不递归重派）"
                    )
                else:
                    logger.warning(
                        f"[MQTT-ACK] 任务 {task.task_id} 拒绝回执重复到达"
                        f"（当前 {task.status}），不再重复回退"
                    )

            # 账本行 + 状态推进同事务提交：任一步失败整体回滚。
            await session.commit()

        # 落库成功后才登记首次规范回执（DB 是权威，跟踪表是去重/审计）
        tracker.record(envelope, outcome=recorded)
        return recorded
    except Exception as exc:   # noqa: BLE001
        # 任何异常都隔离为 warning，绝不打断 MQTT 主循环
        logger.warning(f"[MQTT-ACK] 处理回执异常（已隔离）：{exc}")
        return None


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def _parse_ts(value: Any) -> datetime:
    """解析时间戳，缺省取当前时间。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now()


def _valid_lnglat(value: Any) -> tuple[float | None, float | None]:
    """校验遥测坐标；缺失、非数值或越界一律返回 ``(None, None)``。"""
    if not isinstance(value, dict):
        return None, None
    lng = _safe_float(value.get("lng"))
    lat = _safe_float(value.get("lat"))
    if lng is None or lat is None:
        return None, None
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        return None, None
    return lng, lat


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_int(value: Any) -> int | None:
    number = _safe_float(value)
    return int(number) if number is not None else None


def _device_from_topic(topic: str) -> str | None:
    """从 marine/{site}/{device}/xxx 提取 device_id。"""
    parts = topic.split("/")
    return parts[2] if len(parts) >= 3 and parts[0] == "marine" else None


def _robot_from_topic(topic: str) -> str | None:
    """从 robot/{robot_id}/... 提取 robot_id。"""
    parts = topic.split("/")
    return parts[1] if len(parts) >= 2 and parts[0] == "robot" else None


# 注册表：供 MqttClient 使用
HANDLERS: dict[str, Any] = {
    "handle_event": handle_event,
    "handle_telemetry": handle_telemetry,
    "handle_device_status": handle_device_status,
    "handle_robot_progress": handle_robot_progress,
    "handle_robot_ack": handle_robot_ack,
}
