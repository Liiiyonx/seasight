"""事件相关接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, get_redis_dep, require_operator
from app.db.session import get_session
from app.core.exceptions import ApiResponse
from app.models.event import EventStatus, WasteClass
from app.repositories import EventRepository
from app.schemas import (
    EventIngest,
    EventIngestResult,
    EventOut,
    EventStatusUpdate,
    HeatmapQuery,
)
from app.services.event import EventService

router = APIRouter()


def _to_out(event, lng: float, lat: float) -> EventOut:
    """ORM → 响应体（补充中文标签）。"""
    return EventOut(
        event_id=event.event_id,
        device_id=event.device_id,
        event_time=event.event_time,
        lng=lng,
        lat=lat,
        main_class=event.main_class,
        main_class_label=WasteClass.LABELS.get(event.main_class),
        det_count=event.det_count,
        max_confidence=event.max_confidence,
        evidence_url=event.evidence_url,
        model_version=event.model_version,
        status=event.status,
        status_label={
            "new": "待处理",
            "dispatched": "已派单",
            "resolved": "已清理",
            "ignored": "已忽略",
        }.get(event.status),
        created_at=event.created_at,
    )


@router.post("", response_model=ApiResponse[EventIngestResult], summary="事件上报（HTTP 备用通道）")
async def ingest_event(
    payload: EventIngest,
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis_dep),
):
    """边缘设备上报识别事件。

    主通道是 MQTT；本接口为备用 HTTP 通道，也便于调试与冒烟测试。
    """
    service = EventService(session, redis)
    # 本入口会在下方同步派单，不能再让 ingest 投 Redis Stream ——
    # 否则 dispatch_consumer 会把同一条事件再派一次，撞 uq_task_active_event。
    result = await service.ingest(payload, enqueue_dispatch=False)

    # _try_dispatch 会另开一个会话读事件；若这里不先提交，
    # 新会话读不到本请求刚 flush 但尚未提交的行，同步派单恒为空转。
    await session.commit()

    # 高优先级类别触发派单
    if not result.duplicate and payload.aggregate.main_class in WasteClass.HIGH_PRIORITY:
        from app.mqtt.handlers import _try_dispatch

        # ★ task_id 必须取 _try_dispatch 的返回值。
        #   这里曾写成「查全库最新一条任务」：
        #       tasks = await TaskRepository(session).list_tasks(limit=1)
        #       if tasks[0]: ...  tasks[0][0].task_id
        #   而 list_tasks 返回 tuple[list[Task], int] —— 既没有解包元组，
        #   又拿全库最新任务冒充本次派单结果。后果有两层：
        #     1. 只要库里存在任意一条历史任务，task_created 就恒为 True，
        #        哪怕本次事件根本没派出去（无可用机器人时也报派单成功）；
        #     2. 并发上报时 task_id 指向的是**别人的**任务，前端拿着它去
        #        轮询 /tasks/{task_id} 会看到与本次事件无关的状态。
        task = await _try_dispatch(payload.event_id)
        if task is not None:
            result.task_created = True
            result.task_id = task.task_id

    return ApiResponse.ok(result, message="事件已受理")


@router.get("", response_model=ApiResponse[dict], summary="事件列表")
async def list_events(
    hours: int = Query(24, ge=1, le=720, description="时间窗口（小时）"),
    main_class: str | None = Query(None, description="类别筛选"),
    status: str | None = Query(None, description="状态筛选"),
    device_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """分页查询事件列表（支持时间/类别/状态/设备筛选）。

    operator 角色只看自己辖区（township_scope）的数据，admin/viewer 看全部。
    """
    from sqlalchemy import func, select

    repo = EventRepository(session)
    # 辖区过滤：operator 只看本辖区，admin/viewer 看全部
    township = user.township_scope if user.role == "operator" else None
    events, total = await repo.list_events(
        hours=hours,
        main_class=main_class,
        status=status,
        device_id=device_id,
        township=township,
        limit=page_size,
        offset=(page - 1) * page_size,
    )

    items: list[EventOut] = []
    for event in events:
        row = (
            await session.execute(
                select(func.ST_X(event.location), func.ST_Y(event.location))
            )
        ).first()
        lng = float(row[0]) if row and row[0] is not None else 0.0
        lat = float(row[1]) if row and row[1] is not None else 0.0
        items.append(_to_out(event, lng, lat))

    return ApiResponse.ok(
        {
            "items": [i.model_dump() for i in items],
            "meta": {"total": total, "page": page, "page_size": page_size},
        }
    )


@router.get("/heatmap", response_model=ApiResponse[list], summary="热力图聚合数据")
async def heatmap(
    hours: int = Query(24, ge=1, le=720),
    grid_size: int = Query(500, ge=100, le=5000, description="网格边长（米）"),
    main_class: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """按网格聚合事件密度。

    注意：返回的是单位面积密度（个/m²），不是总数——
    否则大网格天然比小网格热，图会失真。
    """
    service = EventService(session)
    cells = await service.heatmap(hours=hours, grid_size=grid_size, main_class=main_class)
    return ApiResponse.ok(cells)


@router.get("/{event_id}", response_model=ApiResponse[EventOut], summary="事件详情")
async def get_event(event_id: str, session: AsyncSession = Depends(get_session)):
    """查询单条事件详情（含证据帧地址）。"""
    from app.core.exceptions import NotFoundError
    from sqlalchemy import func, select

    event = await EventRepository(session).get_by_event_id(event_id)
    if event is None:
        raise NotFoundError(f"事件 {event_id} 不存在", code=3001)

    row = (
        await session.execute(
            select(func.ST_X(event.location), func.ST_Y(event.location))
        )
    ).first()
    lng = float(row[0]) if row and row[0] is not None else 0.0
    lat = float(row[1]) if row and row[1] is not None else 0.0

    return ApiResponse.ok(_to_out(event, lng, lat))


@router.patch("/{event_id}", response_model=ApiResponse[EventOut], summary="人工更新事件状态（忽略/确认）")
async def update_event_status(
    event_id: str,
    payload: EventStatusUpdate,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(require_operator),
):
    """人工纠误报：把待处理事件标记为 ignored（识别误报，不再派单）
    或 resolved（人工确认已清理，无需机器人）。

    仅允许 new → ignored/resolved。已派单（dispatched）的事件不能直接改，
    否则关联任务会变成孤儿 —— 这类情况须先走工单流程（取消/完成工单）。
    """
    from sqlalchemy import func, select

    from app.core.exceptions import AppException, NotFoundError

    event = await EventRepository(session).get_by_event_id(event_id)
    if event is None:
        raise NotFoundError(f"事件 {event_id} 不存在", code=3001)

    if event.status != EventStatus.NEW:
        raise AppException(
            code=1001,
            message=f"仅「待处理」事件可人工更新（当前为 {event.status}），已派单事件请走工单流程",
            http_status=409,
        )

    event.status = payload.status
    await session.flush()

    from app.services.audit import record_audit

    await record_audit(
        session,
        username=_user.username,
        role=_user.role,
        action="event_status_update",
        target_type="event",
        target_id=event_id,
        detail=f"状态 → {payload.status}",
    )

    row = (
        await session.execute(
            select(func.ST_X(event.location), func.ST_Y(event.location))
        )
    ).first()
    lng = float(row[0]) if row and row[0] is not None else 0.0
    lat = float(row[1]) if row and row[1] is not None else 0.0

    return ApiResponse.ok(_to_out(event, lng, lat), message="事件状态已更新")
