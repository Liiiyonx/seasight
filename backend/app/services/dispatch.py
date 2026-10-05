"""派单引擎 —— 平台最核心的业务逻辑。

对应「感知—决策—执行」闭环中的「决策」环节。

五步筛选逻辑：
    1. 可用性过滤（在线 / 电量 / 仓容）
    2. 距离排序（KNN）
    3. 类别匹配加权（顺路复用）
    4. 选中下发（MQTT + 等 ACK）
    5. ACK 超时处理（换车重派）

防抖：
    同网格区域窗口期内的多个事件合并为一个任务，
    避免机器人被反复派往同一片水域。
"""

from __future__ import annotations

import re
import struct
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import NoRobotAvailableError
from app.core.geo import nearest_township
from app.models.device import Device
from app.models.event import Event, EventStatus
from app.models.task import Task, TaskPriority, TaskStatus
from app.repositories import DeviceRepository, EventRepository, TaskRepository


def _gen_task_id() -> str:
    """生成任务编号：tsk_YYYYMMDD_xxxx。"""
    return f"tsk_{datetime.now():%Y%m%d}_{uuid.uuid4().hex[:6]}"


def _utcnow() -> datetime:
    """任务生命周期统一使用 UTC aware 时间。"""
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """兼容历史库里可能出现的 naive 时间戳。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# 坐标解析
# ----------------------------------------------------------------------
_EWKT_POINT_RE = re.compile(
    r"POINT\s*\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", re.IGNORECASE
)

# EWKB 标志位：0x20000000 = 带 SRID；低 8 位是几何类型（1 = Point）
_EWKB_SRID_FLAG = 0x20000000
_WKB_POINT = 1


def _parse_point(value: Any) -> tuple[float, float] | None:
    """把几何值解析成 `(lng, lat)`；解析不出来返回 None。

    ★ 为什么不再发一条 `SELECT ST_X(...)`：
        任务对象手里本来就持有坐标，为取坐标再跑一次数据库是纯浪费；
        更要紧的是——历史写法 `select(ST_X(task.target_location))` **没有
        `where(...)`**，一旦 `task.target_location` 是列引用，`.first()`
        拿到的就是**全表第一行**的坐标，机器人会被派去别人的目标点，
        而全过程不报错。坐标是内存里已经有的值，就在内存里解析。

    支持两种形态：
        - EWKT 字符串 `SRID=4326;POINT(lng lat)`（新创建任务时的内存值）
        - EWKB 字节 / `WKBElement`（从库里读回的值）
    """
    if value is None:
        return None
    if isinstance(value, str):
        return _parse_ewkt(value)

    raw = getattr(value, "data", None)      # geoalchemy2 的 WKBElement
    if raw is None and isinstance(value, (bytes, bytearray, memoryview)):
        raw = value
    if raw is not None:
        return _parse_ewkb(bytes(raw))
    return None


def _parse_ewkt(text: str) -> tuple[float, float] | None:
    match = _EWKT_POINT_RE.search(text)
    if match is None:
        return None
    return float(match.group(1)), float(match.group(2))


def _parse_ewkb(data: bytes) -> tuple[float, float] | None:
    """解析 (E)WKB 的 Point 类型。只支持点——任务目标点本来就是点。"""
    if len(data) < 5:
        return None
    endian = "<" if data[0] == 1 else ">"
    (gtype,) = struct.unpack_from(endian + "I", data, 1)
    if gtype & 0xFF != _WKB_POINT:
        return None
    offset = 5
    if gtype & _EWKB_SRID_FLAG:
        offset += 4
    if len(data) < offset + 16:
        return None
    x, y = struct.unpack_from(endian + "dd", data, offset)
    return x, y


# ----------------------------------------------------------------------
# 派单收尾（唯一出口）
# ----------------------------------------------------------------------
async def finalize_dispatch(task: Task) -> None:
    """★ 派单成功的**统一收尾**：下发 MQTT 指令 + 推送看板。

    四个派单入口都必须调用它：

        1. `mqtt.handlers._try_dispatch`         —— HTTP 上报后的同步派单
        2. `services.consumer._process_entry`    —— Streams 消费者
        3. `services.consumer.pending_dispatcher`—— 定时补派
        4. `DispatchEngine.assign_pending_tasks` —— 手动补派 API

    ★ 为什么必须收成一处（这是本函数的全部理由）：
        曾经只有前两个入口做了收尾，后两个只创建任务就返回。后果是
        「库里有任务、日志写着补派成功、API 回了 task_ids」，
        而机器人**一条指令都没收到**，看板也不刷新，任务永远停在
        assigned —— 不报错、不抛异常、不打日志，是最难查的一类。
        收尾动作集中到一处后，新加入口不可能再各写各的。

    守卫见 `backend/tests/test_dispatch_finalize.py`：
    用 AST 穷举所有 `dispatch_for_event` 调用点，断言每个都接了本函数。
    """
    from app.mqtt.client import mqtt_client
    from app.ws.manager import ws_manager

    if task.robot_id:
        point = _parse_point(task.target_location)
        if point is None:
            # ★ 拿不到坐标必须留痕：静默跳过会让「机器人为什么没动」无从查起
            logger.error(
                f"[派单] 任务 {task.task_id} 的坐标无法解析"
                f"（类型 {type(task.target_location).__name__}），"
                f"仅推送看板、不下发指令"
            )
        else:
            lng, lat = point
            await mqtt_client.publish_task(
                robot_id=task.robot_id,
                task_id=task.task_id,
                lng=lng,
                lat=lat,
                priority=task.priority,
            )
    else:
        logger.warning(f"[派单] 任务 {task.task_id} 没有绑定机器人，跳过下发指令")

    # ★ 四个字段一个都不能少（契约真源：docs/api.md §「服务端推送消息」）
    await ws_manager.push_task_update(
        {
            "task_id": task.task_id,
            "event_id": task.event_id,
            "robot_id": task.robot_id,
            "status": task.status,
        }
    )


class DispatchEngine:
    """派单引擎。

    用法：
        engine = DispatchEngine(session)
        result = await engine.dispatch_for_event(event)
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.devices = DeviceRepository(session)
        self.events = EventRepository(session)
        self.tasks = TaskRepository(session)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def dispatch_for_event(self, event: Event) -> Task | None:
        """为一条事件尝试派单。

        返回创建的 Task；若合并到已有任务则返回该任务；
        若该事件已有在途任务则返回 None。

        ★ 无可用机器人时**抛** `NoRobotAvailableError`（不是返回 None）。
        这里曾写成「返回 None」，与实现不符——照着 docstring 写调用方
        就不会 try/except，异常会一路穿到 HTTP 层变成 500。
        """
        # ---------- 前置：防重复派单 ----------
        if await self.tasks.has_active_task_for_event(event.event_id):
            logger.info(f"[派单] 事件 {event.event_id} 已有在途任务，跳过")
            return None

        # ---------- 防抖：同区域窗口内合并 ----------
        merged = await self._try_merge(event)
        if merged is not None:
            logger.info(f"[派单] 事件 {event.event_id} 合并至任务 {merged.task_id}")
            await self._mark_event(event, EventStatus.DISPATCHED)
            return merged

        # ---------- 五步筛选 ----------
        robot = await self._select_robot(event)
        if robot is None:
            logger.warning(f"[派单] 事件 {event.event_id} 无可用机器人，保持待派单")
            raise NoRobotAvailableError("当前无满足条件的可用机器人（在线/电量/仓容）")

        task = await self._create_and_assign(event, robot)
        await self._mark_event(event, EventStatus.DISPATCHED)

        logger.info(
            f"[派单] 事件 {event.event_id} → 任务 {task.task_id} → 机器人 {robot.device_id}"
        )
        return task

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    async def _try_merge(self, event: Event) -> Task | None:
        """防抖合并：查找窗口期内附近的活跃任务。

        合并判断需要知道「当时有没有在途任务」，所以必须放在引擎层而非事件层。
        """
        # 取事件坐标
        lng, lat = await self._event_lnglat(event)
        if lng is None or lat is None:
            return None

        return await self.tasks.find_mergeable_task(
            lng=lng,
            lat=lat,
            window_minutes=settings.dispatch_merge_window_minutes,
            radius_m=settings.dispatch_merge_radius_meters,
        )

    async def _select_robot(self, event: Event) -> Device | None:
        """五步筛选的前三步：可用性过滤 → 距离排序 → 类别匹配加权。

        锚点是**事件**坐标 —— `find_nearest_available_robots` 用 event_id
        反查 `t_event.location`。
        """
        return await self._select_robot_at_point(
            event_id=event.event_id,
            event=event,
        )

    async def _select_robot_at_point(
        self,
        *,
        event_id: str | None = None,
        lng: float | None = None,
        lat: float | None = None,
        event: Event | None = None,
        exclude_robot_ids: set[str] | None = None,
    ) -> Device | None:
        """选机器人的**唯一实现**：从候选池挑一台。

        两种入参二选一：

            - `event_id` + `event`：事件派单，锚点是事件坐标；
            - `lng` / `lat`：**人工建单**的工单（`event_id IS NULL`），
              锚点直接来自 `t_task.target_location`。

        ★ 为什么要把「候选要怎么筛、怎么加权、怎么排序」收在一处：
            这段规则（电量阈值、仓容阈值、类别加权、距离兜底）只有一份时，
            改策略才不会出现「事件派单按新规则、人工补派按旧规则」的静默分叉。
            两条入参路径的差别仅在「锚点从哪来」这一步。
        """
        if event_id is not None:
            candidates = await self.devices.find_nearest_available_robots(
                event_id,
                max_distance_m=settings.dispatch_range_meters,
                limit=5,
                exclude_robot_ids=exclude_robot_ids,
            )
        else:
            if lng is None or lat is None:
                logger.error("[派单] 选机器人时既没有 event_id 也没有坐标，跳过")
                return None
            candidates = await self.devices.find_nearest_available_robots_at(
                lng,
                lat,
                max_distance_m=settings.dispatch_range_meters,
                limit=5,
                exclude_robot_ids=exclude_robot_ids,
            )

        if not candidates:
            return None

        # 过滤出真正可用的（电量 / 仓容）
        usable: list[tuple[Device, float, int]] = []
        for device, distance in candidates:
            meta = device.meta or {}
            battery = int(meta.get("battery", 100))
            bins = meta.get("bins", {}) or {}
            total_usage = sum(float(v) for v in bins.values())

            if battery < settings.dispatch_min_battery:
                logger.debug(f"[派单] {device.device_id} 电量 {battery}% 不足，跳过")
                continue
            if total_usage >= settings.dispatch_max_bin_usage:
                logger.debug(f"[派单] {device.device_id} 仓容 {total_usage:.0%} 已满，跳过")
                continue

            # 类别匹配加权：正在处理**同类别**任务的机器人优先（顺路复用）。
            # 人工建单的工单没有 event（也就没有 main_class），无从配对，
            # 一律给普通权重 1 —— 排序退化为「按距离最近」，是合理缺省。
            weight = await self._class_match_weight(event, device.device_id) if event else 1

            usable.append((device, distance, weight))

        if not usable:
            return None

        # 排序：类别匹配优先（0 优于 1），其次距离最近
        usable.sort(key=lambda x: (x[2], x[1]))
        return usable[0][0]

    async def _class_match_weight(self, event: Event, robot_id: str) -> int:
        """类别匹配加权：0=该机器人已在处理同类别任务（更优），1=普通。

        ★ 必须**两两比对** event 与机器人现有任务的类别。

        这里曾写成 `return 0 if event.main_class == FOAM else 1` ——
        收了 `task` 形参却从未使用，于是加权退化成「按事件类别给常量」，
        与机器人手上在跑什么完全无关。后果：
          a. 文档承诺的「同类别顺路复用」**从未生效**（空驶优化是空的）；
          b. 因为返回的常量对同一 event 的所有候选机器人完全相同，
             排序实际只剩「距离」一维 —— 看起来在工作，实则白算。

        任务的类别取自它关联事件的 `main_class`。
        """
        active_tasks = await self.tasks.list_active_tasks_for_robot(robot_id)
        if not active_tasks:
            return 1   # 手上没任务 → 无复用可言，普通权重

        active_event_ids = [t.event_id for t in active_tasks if t.event_id]
        if not active_event_ids:
            return 1

        classes = await self.events.list_main_classes_for_events(active_event_ids)
        # 该机器人任一在途任务的类别与本次事件相同 → 顺路，加权
        return 0 if event.main_class in classes else 1

    async def _create_and_assign(self, event: Event, robot: Device) -> Task:
        """第四步：创建任务记录并下发（MQTT 下发由 mqtt 层订阅后执行）。"""
        lng, lat = await self._event_lnglat(event)
        now = _utcnow()

        # ★ 优先级取自单一真源 TaskPriority，不在此写 `1 if ... else 3`
        priority = TaskPriority.for_waste_class(event.main_class)

        task = await self.tasks.create(
            task_id=_gen_task_id(),
            event_id=event.event_id,
            robot_id=robot.device_id,
            target_location=f"SRID=4326;POINT({lng} {lat})",
            status=TaskStatus.ASSIGNED,
            priority=priority,
            assigned_at=now,
            township=event.township or (
                nearest_township(lng, lat) if lng is not None and lat is not None else None
            ),
        )

        # 更新机器人状态（忙碌中）
        meta: dict[str, Any] = dict(robot.meta or {})
        meta["current_task_id"] = task.task_id
        robot.meta = meta

        return task

    async def _mark_event(self, event: Event, status: str) -> None:
        event.status = status

    async def _event_lnglat(self, event: Event) -> tuple[float | None, float | None]:
        """读取事件坐标（PostGIS geometry → 经纬度）。"""
        point = _parse_point(event.location)
        if point is None:
            return None, None
        return point

    # ------------------------------------------------------------------
    # 状态机流转
    # ------------------------------------------------------------------
    async def transition(
        self,
        task: Task,
        target_status: str,
        *,
        robot_id: str | None = None,
        collected_weight: float | None = None,
        review_result: str | None = None,
        remark: str | None = None,
    ) -> Task:
        """任务状态迁移（唯一合法入口）。

        ★ 禁止在别处直接改 task.status —— 所有变更必须经过此方法校验。
        """
        from app.core.exceptions import InvalidStateTransitionError

        current = task.status
        if not TaskStatus.can_transition(current, target_status):
            raise InvalidStateTransitionError(current, target_status)

        now = _utcnow()
        task.status = target_status

        if robot_id is not None:
            task.robot_id = robot_id

        # 时间戳链
        if target_status == TaskStatus.ASSIGNED and task.assigned_at is None:
            task.assigned_at = now
        elif target_status == TaskStatus.NAVIGATING:
            task.ack_at = task.ack_at or now
        elif target_status == TaskStatus.COLLECTING:
            task.started_at = task.started_at or now
        elif target_status == TaskStatus.DONE:
            task.finished_at = now

        if collected_weight is not None:
            task.collected_weight = collected_weight
        if review_result is not None:
            task.review_result = review_result
        if remark is not None:
            task.remark = remark

        logger.info(f"[状态机] 任务 {task.task_id}: {current} → {target_status}")

        # 任务完成 → 同步事件状态
        if target_status == TaskStatus.DONE and task.event_id:
            event = await self.events.get_by_event_id(task.event_id)
            if event is not None:
                event.status = EventStatus.RESOLVED

        return task

    async def handle_ack_timeout(self, task: Task) -> bool:
        """第五步：ACK 超时处理。

        机器人 15 秒未确认，任务退回待派单池，换下一台。
        返回 True 表示已回退。
        """
        if task.status != TaskStatus.ASSIGNED:
            return False
        if task.assigned_at is None:
            return False

        elapsed = _utcnow() - _as_utc(task.assigned_at)
        if elapsed < timedelta(seconds=settings.dispatch_ack_timeout_seconds):
            return False

        logger.warning(f"[派单] 任务 {task.task_id} ACK 超时（{elapsed.total_seconds():.0f}s），回退待派单")
        await self.transition(task, TaskStatus.PENDING, remark="ACK 超时，重新派单")
        return True

    async def assign_pending_tasks(self, limit: int = 10) -> list[Task]:
        """扫描待派单对象并尝试补派（补派 API 与定时任务调用）。

        扫描**两类**对象，两类都要覆盖：

            1. `t_event` 中状态仍为 `new` 的高优先级事件 ——
               当初无可用机器人，事件保持 new；机器人上线后由
               `dispatch_for_event` 建一条**新**任务派出去；
            2. `t_task` 中 `status='pending' AND robot_id IS NULL` 的工单 ——
               **人工建单**（`POST /tasks` 不指定机器人）产生的工单。
               它们 `event_id` 为 NULL，没有任何事件可扫，第一类永远扫不到。

        ★ 第二类是 2026-09-27 线上验收发现的缺陷（报告 P1-3）：
            人工建单 → 点「触发补派」→ 接口回 `{"dispatched": 0}` 且提示
            「完成补派 0 个任务」，工单永远停在待派单，既不报错也不留日志。
            根因就是这里只扫了事件表。

        两类共用同一处收尾（见下方注释），调用方无需区分来源。
        """
        created: list[Task] = []
        events = await self.events.recent_for_dispatch(limit=limit)
        for event in events:
            if await self.tasks.has_active_task_for_event(event.event_id):
                continue
            try:
                task = await self.dispatch_for_event(event)
                if task is not None:
                    created.append(task)
            except NoRobotAvailableError:
                continue   # 暂无可用机器人，等下一轮

        # ★ 第二类：人工建单产生的「无事件待派单工单」。
        #   放在第一类之后，让事件派单优先占用机器人 —— 事件由摄像头自动
        #   发现、有明确的时空证据；人工建单是平台判读后的补充指令。
        created.extend(await self._assign_unassigned_pending(limit=limit))

        # ★ 收尾（下发 + 推送）必须在循环**之后**统一做：
        #   放在循环内的话，一旦后面的事件抛异常触发 rollback，
        #   前面已经下发过的任务会与数据库状态不一致。
        for task in created:
            await finalize_dispatch(task)
        return created

    async def _assign_unassigned_pending(self, limit: int = 10) -> list[Task]:
        """把 `status='pending' AND robot_id IS NULL` 的工单派出去。

        ★ 为什么不复用 `dispatch_for_event`：
            那个入口以 **event** 为中心 —— 防抖合并（`_try_merge`）、
            事件状态回写（`_mark_event`）都挂在它上面。人工建单的工单
            `event_id` 就是空的，走那条路要么被跳过、要么得先伪造一条事件。
            本方法直接改**已有**工单的 robot_id / status，不新建任务。

        与 `_create_and_assign` 保持等价的赋值口径（robot_id、
        `assigned_at` 重置、机器人 `meta.current_task_id`），
        确保工单无论是自动派还是补派出去的，字段状态都一致。
        """
        tasks = await self.tasks.list_unassigned_pending(limit=limit)
        if not tasks:
            return []

        assigned: list[Task] = []
        taken: set[str] = set()   # 本轮已派出去的机器人，见下
        for task in tasks:
            point = _parse_point(task.target_location)
            if point is None:
                # ★ 坐标取不到必须留痕。`target_location` 是 NOT NULL 列，
                #   走到这里说明几何值解析失败（异常写入 / 新几何类型），
                #   静默 continue 会让人以为「补派功能又坏了」。
                logger.error(
                    f"[补派] 工单 {task.task_id} 的目标坐标无法解析"
                    f"（类型 {type(task.target_location).__name__}），跳过"
                )
                continue
            lng, lat = point

            robot = await self._select_robot_at_point(
                lng=lng, lat=lat, exclude_robot_ids=taken
            )
            if robot is None:
                logger.info(
                    f"[补派] 工单 {task.task_id} 附近无可用机器人"
                    f"（{lng:.5f},{lat:.5f}），保持待派单"
                )
                continue

            await self.transition(task, TaskStatus.ASSIGNED, robot_id=robot.device_id)
            # ★ 与 `reassign_task` 同理：`transition` 只在 `assigned_at`
            #   为空时才赋值，这里显式写成本轮时间，ACK 超时判定才有起点。
            task.assigned_at = _utcnow()

            meta: dict[str, Any] = dict(robot.meta or {})
            meta["current_task_id"] = task.task_id
            robot.meta = meta

            taken.add(robot.device_id)
            assigned.append(task)
            logger.info(f"[补派] 工单 {task.task_id} → 机器人 {robot.device_id}")

        return assigned

    async def reassign_task(self, task: Task) -> bool:
        """把 PENDING 任务重新派给一台可用机器人（「换车重派」）。

        ★ 与 `handle_ack_timeout` 配套：那一步只把卡住的任务退回 PENDING，
        退回之后**必须有人把它再派出去**，否则任务只是换个状态继续卡着。
        这两步过去都没有任何调用点，于是文档里「五步筛选」的第五步
        （ACK 超时处理）从未真正生效过——机器人掉线后任务会永久停在
        assigned，且没有任何日志。
        """
        if task.status != TaskStatus.PENDING:
            return False
        if not task.event_id:
            return False

        event = await self.events.get_by_event_id(task.event_id)
        if event is None:
            return False

        robot = await self._select_robot(event)
        if robot is None:
            return False

        await self.transition(task, TaskStatus.ASSIGNED, robot_id=robot.device_id)
        # ★ 必须重置计时起点：`transition` 只在 `assigned_at` 为空时才赋值，
        #   沿用旧值会让这个任务下一轮立刻又被判成超时，
        #   于是每 30 秒空转重派一次，永远收敛不了。
        task.assigned_at = _utcnow()

        await finalize_dispatch(task)
        return True
