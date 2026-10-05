"""机器人相关接口（实时状态监控）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApiResponse
from app.db.session import get_session
from app.repositories import DeviceRepository, TaskRepository
from app.schemas import RobotOut
from app.services.dispatch import _parse_point

router = APIRouter()


@router.get("", response_model=ApiResponse[list], summary="机器人列表与实时状态")
async def list_robots(session: AsyncSession = Depends(get_session)):
    """查询全部机器人状态（含电量、三仓占用、当前任务）。

    bins 是「打捞即粗分三仓」设计的软件侧落点——
    没有仓容数据，三仓设计在平台上就是空的。
    """
    devices = await DeviceRepository(session).list_robots()
    task_repo = TaskRepository(session)

    items: list[RobotOut] = []
    for device in devices:
        point = _parse_point(device.location)
        lng, lat = point if point is not None else (0.0, 0.0)

        meta = device.meta or {}
        active_tasks = await task_repo.list_active_tasks_for_robot(device.device_id)

        items.append(
            RobotOut(
                robot_id=device.device_id,
                name=device.name,
                lng=lng,
                lat=lat,
                status=device.status,
                battery=int(meta.get("battery", 0)) or None,
                bins={k: float(v) for k, v in (meta.get("bins") or {}).items()},
                current_task_id=active_tasks[0].task_id if active_tasks else None,
                last_heartbeat=device.last_heartbeat,
            )
        )

    return ApiResponse.ok([i.model_dump() for i in items])


@router.get("/{robot_id}", response_model=ApiResponse[RobotOut], summary="机器人详情")
async def get_robot(robot_id: str, session: AsyncSession = Depends(get_session)):
    from app.core.exceptions import NotFoundError

    device = await DeviceRepository(session).get_robot_by_id(robot_id)
    if device is None:
        raise NotFoundError(f"机器人 {robot_id} 不存在", code=2001)

    point = _parse_point(device.location)
    lng, lat = point if point is not None else (0.0, 0.0)

    meta = device.meta or {}
    active_tasks = await TaskRepository(session).list_active_tasks_for_robot(robot_id)

    return ApiResponse.ok(
        RobotOut(
            robot_id=device.device_id,
            name=device.name,
            lng=lng,
            lat=lat,
            status=device.status,
            battery=int(meta.get("battery", 0)) or None,
            bins={k: float(v) for k, v in (meta.get("bins") or {}).items()},
            current_task_id=active_tasks[0].task_id if active_tasks else None,
            last_heartbeat=device.last_heartbeat,
        )
    )
