"""后台消费者与定时任务。

两个常驻 asyncio task：
1. dispatch_consumer —— 消费 Redis Streams 中的事件，执行派单
2. pending_dispatcher —— 定时补派（处理机器人离线期间积压的事件）

为什么用 Redis Streams 而不是：
- 数据库轮询：延迟高、空转耗资源
- 同步调用派单：机器人离线会阻塞 HTTP 请求
- Kafka/RabbitMQ：对小团队是过度设计，Redis 本来就要用

Streams 相比 pub/sub 的优势：消费者组 + ACK + pending list，消息不会丢。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from loguru import logger

from app.core.config import settings
from app.core.deps import get_redis
from app.db.session import get_session_factory


# PEL 回收的最小空闲时间：消息挂起超过这么久才认领回来重试。
# 不能设 0 —— 否则会把「刚投递、正在处理」的消息也抢回来重复处理。
PEL_MIN_IDLE_MS = 60_000


def _stream_feature_unsupported(exc: BaseException) -> bool:
    """判断 Redis 是否缺少 Streams 消费组/认领能力。

    开发机上可能误连旧版 Redis（没有 XGROUP/XAUTOCLAIM）。这不是业务
    处理失败，不能让后台任务每 3 秒刷一次同样的错误；应明确降级到
    数据库补派（pending_dispatcher）。
    """
    message = str(exc).lower()
    return "unknown command" in message and (
        "xgroup" in message or "xautoclaim" in message or "xreadgroup" in message
    )


async def _reclaim_pending(
    redis,
    stream: str,
    group: str,
    consumer_name: str,
    min_idle_ms: int = PEL_MIN_IDLE_MS,
) -> list[tuple[str, dict]]:
    """认领 PEL（Pending Entries List）里挂起过久的消息，返回待处理条目。

    ★ 为什么必须有这一段 —— 这是 Redis Streams 最经典的坑：

        `xreadgroup(streams={stream: ">"})` **只读新消息**，
        永远不返回 PEL 里的条目。而 `_process_entry` 在「无可用机器人」时
        刻意**不 ACK**，把消息留在 PEL 等后续重试。两者一叠加：
          - 这些消息**永远不会被重投**（哪怕机器人后来上线了）；
          - PEL 只增不减，且**不受 stream 的 maxlen 裁剪影响**；
          - 功能上被定时补派从数据库兜住了，所以**从外面完全看不出来**。

        于是「留着重试」实际上变成了「永久滞留 + 内存缓慢泄漏」。

    认领回来的条目会走一遍 `_process_entry`：
      - 事件仍未派单且有机器人了 → 正常派单并 ACK；
      - 事件已被定时补派处理掉 → 状态不是 NEW，直接 ACK，PEL 随之清空。

    换言之，这一段同时解决了「重试」和「PEL 清理」两件事。
    """
    # redis-py 8.x 签名：xautoclaim(name, groupname, consumername, min_idle_time, ...)
    result = await redis.xautoclaim(
        stream, group, consumer_name, min_idle_ms, count=10
    )
    # 返回 [next_start_id, entries, deleted_ids]；只要中间那段条目
    if not isinstance(result, (list, tuple)) or len(result) < 2:
        return []
    entries = result[1] or []
    return [(entry_id, fields) for entry_id, fields in entries]


async def dispatch_consumer() -> None:
    """派单消费者：从 Redis Stream 读取事件并执行派单。

    使用消费者组（consumer group）保证消息不丢；
    处理失败进入死信 stream 供人工排查。
    """
    from app.core.exceptions import NoRobotAvailableError
    from app.models.event import EventStatus
    from app.models.task import TaskStatus
    from app.repositories import EventRepository, TaskRepository
    from app.services.dispatch import DispatchEngine, finalize_dispatch

    stream = settings.redis_stream_events
    group = settings.redis_consumer_group
    consumer_name = f"worker-{datetime.now():%H%M%S}"

    # 等待 Redis 就绪
    redis = None
    for _ in range(10):
        try:
            redis = await get_redis()
            await redis.ping()
            break
        except Exception:   # noqa: BLE001
            await asyncio.sleep(2)

    if redis is None:
        logger.error("[消费者] Redis 不可用，派单消费者退出")
        return

    # 创建消费者组（幂等）
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
        logger.info(f"[消费者] 创建消费者组 {group} on {stream}")
    except Exception as exc:   # noqa: BLE001
        if _stream_feature_unsupported(exc):
            logger.warning(
                "[消费者] 当前 Redis 不支持消费组/XAUTOCLAIM，"
                "已切换为数据库定时补派；Redis Streams 消费未启用"
            )
            return
        if "BUSYGROUP" not in str(exc):
            logger.warning(f"[消费者] 创建消费者组失败：{exc}")

    logger.info(f"[消费者] 派单消费者已启动：{consumer_name}")

    while True:
        try:
            # ---------- 先回收挂起过久的消息（PEL）----------
            # ★ 放在读新消息之前：这些条目已经等了一轮，优先级更高。
            for entry_id, fields in await _reclaim_pending(
                redis, stream, group, consumer_name
            ):
                await _process_entry(
                    redis, entry_id, fields, stream, group,
                    DispatchEngine, EventRepository, EventStatus,
                )

            # ---------- 再阻塞读取新消息（block=5000ms 便于响应取消）----------
            messages = await redis.xreadgroup(
                groupname=group,
                consumername=consumer_name,
                streams={stream: ">"},
                count=10,
                block=5000,
            )

            if not messages:
                continue

            for _stream_name, entries in messages:
                for entry_id, fields in entries:
                    await _process_entry(
                        redis, entry_id, fields, stream, group, DispatchEngine, EventRepository, EventStatus
                    )

        except asyncio.CancelledError:
            logger.info("[消费者] 派单消费者收到取消信号，退出")
            break
        except Exception as exc:   # noqa: BLE001
            if _stream_feature_unsupported(exc):
                logger.warning(
                    "[消费者] 当前 Redis 不支持 Streams 消费组，"
                    "已切换为数据库定时补派；Redis Streams 消费未启用"
                )
                return
            logger.error(f"[消费者] 循环异常：{exc}")
            await asyncio.sleep(3)


async def _process_entry(
    redis,
    entry_id: str,
    fields: dict,
    stream: str,
    group: str,
    DispatchEngine,
    EventRepository,
    EventStatus,
) -> None:
    """处理单条队列消息。"""
    from app.core.exceptions import NoRobotAvailableError
    from app.services.dispatch import finalize_dispatch

    event_id = fields.get("event_id")
    if not event_id:
        await redis.xack(stream, group, entry_id)
        return

    try:
        async with get_session_factory()() as session:
            repo = EventRepository(session)
            event = await repo.get_by_event_id(event_id)

            if event is None or event.status != EventStatus.NEW:
                # 事件不存在或已处理，直接 ACK
                await redis.xack(stream, group, entry_id)
                return

            engine = DispatchEngine(session)
            task = await engine.dispatch_for_event(event)
            await session.commit()

            if task is not None:
                # ★ 下发 + 推送走统一收尾
                await finalize_dispatch(task)

            await redis.xack(stream, group, entry_id)

    except NoRobotAvailableError:
        # 无可用机器人：不 ACK，留在 pending list 等下次补派
        logger.warning(f"[消费者] 事件 {event_id} 暂无可用机器人，稍后重试")
    except Exception as exc:   # noqa: BLE001
        logger.exception(f"[消费者] 处理事件 {event_id} 失败：{exc}")
        # 进死信队列，人工排查
        try:
            await redis.xadd(
                settings.redis_dead_letter,
                {"event_id": event_id, "error": str(exc)[:500], "entry_id": entry_id},
                maxlen=1000,
                approximate=True,
            )
            await redis.xack(stream, group, entry_id)
        except Exception:   # noqa: BLE001
            pass


async def pending_dispatcher() -> None:
    """定时补派：处理机器人离线期间积压的待派单事件。

    每 30 秒扫描一次，把新事件派给新上线的机器人。
    """
    from app.core.exceptions import NoRobotAvailableError
    from app.models.event import EventStatus
    from app.models.task import TaskStatus
    from app.repositories import EventRepository, TaskRepository
    from app.services.dispatch import DispatchEngine, finalize_dispatch

    # 启动延迟，避免与服务启动竞争
    await asyncio.sleep(10)
    logger.info("[补派] 定时补派任务已启动（间隔 30 秒）")

    while True:
        try:
            session_factory = get_session_factory()

            # 先只快照标量 ID。ORM rollback 会过期对象属性，若在失败后
            # 继续从上一轮的 Task/Event 上读 ID，会抛 MissingGreenlet，
            # 把单条失败升级为整轮循环失败。
            async with session_factory() as session:
                assigned_ids = [
                    task.task_id
                    for task in await TaskRepository(session).list_by_status(
                        TaskStatus.ASSIGNED, limit=50
                    )
                ]
                pending_ids = [
                    task.task_id
                    for task in await TaskRepository(session).list_by_status(
                        TaskStatus.PENDING, limit=50
                    )
                ]
                event_ids = [
                    event.event_id
                    for event in await EventRepository(session).recent_for_dispatch(limit=20)
                ]

            # ---------- 第一步：ACK 超时回退（五步筛选的第五步）----------
            # ★ `handle_ack_timeout` 过去没有任何调用点：机器人掉线后
            #   任务会永久停在 assigned，不报错也不打日志。
            for task_id in assigned_ids:
                try:
                    async with session_factory() as session:
                        task = await TaskRepository(session).get_by_task_id(task_id)
                        if task is None:
                            continue
                        engine = DispatchEngine(session)
                        if await engine.handle_ack_timeout(task):
                            await session.commit()
                except Exception as exc:   # noqa: BLE001
                    logger.warning(f"[补派] 任务 {task_id} 超时回退失败：{exc}")

            # ---------- 第二步：换车重派 ----------
            # 回退成 pending 的任务若不重新派出去，只是换个状态继续卡着。
            for task_id in pending_ids:
                try:
                    async with session_factory() as session:
                        task = await TaskRepository(session).get_by_task_id(task_id)
                        if task is None:
                            continue
                        engine = DispatchEngine(session)
                        if await engine.reassign_task(task):
                            await session.commit()
                except Exception as exc:   # noqa: BLE001
                    logger.warning(f"[补派] 任务 {task_id} 换车重派失败：{exc}")

            # ---------- 第三步：为尚未派单的事件补派 ----------
            dispatched = 0
            for event_id in event_ids:
                try:
                    async with session_factory() as session:
                        event = await EventRepository(session).get_by_event_id(event_id)
                        if event is None or event.status != EventStatus.NEW:
                            continue
                        engine = DispatchEngine(session)
                        task = await engine.dispatch_for_event(event)
                        if task is None:
                            continue
                        await session.commit()
                        # ★ 补派也必须下发 + 推送。这里曾经只建任务不通知，
                        #   于是「日志说补派成功」而机器人一动不动。
                        await finalize_dispatch(task)
                        dispatched += 1
                except NoRobotAvailableError:
                    continue   # 暂无可用机器人，下一轮再试
                except Exception as exc:   # noqa: BLE001
                    logger.warning(f"[补派] 事件 {event_id} 处理失败：{exc}")
                    continue

            if dispatched:
                logger.info(f"[补派] 本轮补派 {dispatched} 个任务")

        except asyncio.CancelledError:
            logger.info("[补派] 定时任务收到取消信号，退出")
            break
        except Exception as exc:   # noqa: BLE001
            logger.opt(exception=exc).error(f"[补派] 循环异常：{exc}")

        await asyncio.sleep(30)
