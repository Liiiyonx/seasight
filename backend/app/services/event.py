"""事件服务：接收上报 → 幂等判重 → 落库 → 投递派单队列 → 推送大屏。

这是平台数据流的入口。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException, ErrorCode
from app.core.geo import nearest_township
from app.models.event import Event, EventStatus, WasteClass
from app.repositories import DeviceRepository, EventRepository
from app.schemas import EventIngest, EventIngestResult


class EventService:
    """事件处理服务。"""

    def __init__(self, session: AsyncSession, redis: Redis | None = None) -> None:
        self.session = session
        self.redis = redis
        self.events = EventRepository(session)
        self.devices = DeviceRepository(session)

    async def ingest(
        self, payload: EventIngest, *, enqueue_dispatch: bool = True
    ) -> EventIngestResult:
        """接收一条事件上报。

        幂等保证：
            1. (device_id, seq) 唯一约束 —— 数据库层面兜底
            2. 此处先查一次 —— 快速返回，避免触发约束异常

        enqueue_dispatch=False 表示调用方会自己执行同步派单（HTTP/MQTT
        入口当前都是这个模式）：事件只落库，不再投 Redis Stream，避免
        同一条事件被同步派单与后台消费者重复处理。

        QoS 1 会重复投递，所以判重是必需的，不是可选优化。
        """
        # ---------- 幂等判重 ----------
        if await self.events.exists_by_device_seq(payload.device_id, payload.seq):
            logger.info(
                f"[事件] 重复上报 device={payload.device_id} seq={payload.seq}，忽略"
            )
            return EventIngestResult(
                accepted=True,
                event_id=payload.event_id,
                duplicate=True,
                message="重复上报（device_id+seq 已存在），已忽略",
            )

        if await self.events.get_by_event_id(payload.event_id) is not None:
            return EventIngestResult(
                accepted=True,
                event_id=payload.event_id,
                duplicate=True,
                message="重复上报（event_id 已存在），已忽略",
            )

        # ---------- 校验设备 ----------
        device = await self.devices.get_by_device_id(payload.device_id)
        if device is None:
            raise AppException(
                code=ErrorCode.DEVICE_NOT_FOUND,
                message=f"设备 {payload.device_id} 未注册",
            )

        # ---------- 落库 ----------
        event = await self.events.create(
            event_id=payload.event_id,
            device_id=payload.device_id,
            event_time=payload.timestamp,
            location=f"SRID=4326;POINT({payload.location.lng} {payload.location.lat})",
            main_class=payload.aggregate.main_class,
            det_count=payload.aggregate.count,
            max_confidence=payload.aggregate.max_confidence,
            evidence_url=payload.evidence_url,
            model_version=payload.model_version or settings.ai_model_version,
            seq=payload.seq,
            status=EventStatus.NEW,
            township=nearest_township(payload.location.lng, payload.location.lat),
        )

        # 更新设备心跳（事件本身就是活跃证明）
        device.last_heartbeat = datetime.now()
        if device.status != "online":
            device.status = "online"

        await self.session.flush()

        # ---------- 投递派单队列 ----------
        # 只有调用方没有同步派单时才入队；同步派单入口必须传 False，
        # 否则同一事件会被 dispatch_consumer 再派一次。
        if enqueue_dispatch:
            await self._enqueue_dispatch(event)

        logger.info(
            f"[事件] 入库 {event.event_id} 类别={WasteClass.LABELS.get(event.main_class, event.main_class)} "
            f"设备={event.device_id} 数量={event.det_count}"
        )

        return EventIngestResult(
            accepted=True,
            event_id=event.event_id,
            duplicate=False,
            message="事件已受理",
        )

    async def _enqueue_dispatch(self, event: Event) -> None:
        """把事件推入 Redis Stream，交给派单消费者处理（显式异步通道）。

        为什么用 Redis Streams 而不是：
        - 数据库轮询：延迟高、空转耗资源
        - 同步调用派单：机器人离线会阻塞 HTTP 请求
        - Kafka/RabbitMQ：对小团队是过度设计，Redis 本来就要用

        ★ 当前 HTTP/MQTT 两个入口都在提交后同步派单，调用 ingest 时
          必须传 enqueue_dispatch=False，不要在这里重复入队。本方法
          保留给「不需要同步 task_id 的异步入口」和批量补投使用。
        """
        if self.redis is None:
            return

        try:
            await self.redis.xadd(
                settings.redis_stream_events,
                {
                    "event_id": event.event_id,
                    "device_id": event.device_id,
                    "main_class": event.main_class,
                    "ts": datetime.now().isoformat(),
                },
                maxlen=10000,   # 防止 stream 无限增长
                approximate=True,
            )
        except Exception as exc:   # noqa: BLE001
            # 队列投递失败不应阻断事件入库，由定时补派兜底
            logger.error(f"[事件] 投递派单队列失败：{exc}")

    async def heatmap(
        self,
        *,
        hours: int = 24,
        grid_size: int = 500,
        main_class: str | None = None,
    ) -> list[dict[str, Any]]:
        """热力图聚合数据。"""
        return await self.events.heatmap_aggregate(
            hours=hours, grid_size=grid_size, main_class=main_class
        )

    async def event_lnglat(self, event: Event) -> tuple[float | None, float | None]:
        """读取事件经纬度。"""
        from sqlalchemy import select

        stmt = select(func.ST_X(event.location), func.ST_Y(event.location))
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None, None
        return (
            float(row[0]) if row[0] is not None else None,
            float(row[1]) if row[1] is not None else None,
        )
