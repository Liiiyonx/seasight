"""工单执行仿真接口。

仿真引擎只生成设备侧报文并调用正式 MQTT handlers；本模块负责访问控制、
运行控制和轨迹读取，不直接修改任务状态。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_operator
from app.core.exceptions import ApiResponse, AppException, ErrorCode, NotFoundError
from app.db.session import get_session
from app.models.misc import Track
from app.models.task_ack import TaskAck
from app.repositories import TaskRepository
from app.services.dispatch import _parse_point
from app.services.sim import simulation_manager

router = APIRouter()


# ACK 审计账本 outcome → 人话。与 app.mqtt.ack.AckResult 的取值一一对应。
_ACK_OUTCOME_LABEL = {
    "new": "首次回执，状态已推进",
    "duplicate": "重复回执，不重复推进状态",
    "late": "迟到回执（超出等待窗口）",
    "out_of_order": "乱序回执（序号倒退）",
}


class SimulationStartRequest(BaseModel):
    speed: float = 1.0


class SimulationSpeedRequest(BaseModel):
    speed: float


class SimulationStopRequest(BaseModel):
    reason: str = ""


def _normalize_speed(value: float) -> float:
    speed = float(value)
    if speed not in (1.0, 2.0, 4.0):
        raise AppException(
            code=ErrorCode.PARAM_INVALID,
            message="仿真倍速只支持 1、2、4",
            http_status=422,
        )
    return speed


async def _authorized_task(
    session: AsyncSession,
    task_id: str,
    user: CurrentUser,
):
    task = await TaskRepository(session).get_by_task_id(task_id)
    if task is None:
        raise NotFoundError(f"任务 {task_id} 不存在", code=ErrorCode.TASK_NOT_FOUND)
    if user.role == "operator" and task.township != user.township_scope:
        raise AppException(
            code=ErrorCode.FORBIDDEN,
            message="operator 只能操作本辖区任务的仿真",
            http_status=403,
        )
    return task


def _aware(value: datetime) -> datetime:
    """把可能为 naive 的时间戳按 UTC 解释，保证 log 条目能稳定排序。

    PG 的 TIMESTAMPTZ 读回来是 aware；SQLite / 历史数据可能是 naive。
    两者混在一个 sort key 里会抛 `TypeError: can't compare offset-naive
    and offset-aware datetimes` —— 一条日志都发不出去。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _load_ack_rows(session: AsyncSession, task_id: str) -> list[TaskAck]:
    """取该工单的 ACK 审计账本行（真实回执证据，非仿真产生）。"""
    stmt = (
        select(TaskAck)
        .where(TaskAck.task_id == task_id)
        .order_by(TaskAck.received_wall_at, TaskAck.id)
    )
    return list((await session.execute(stmt)).scalars().all())


def _history_logs(
    task,
    trajectory: list[dict[str, Any]],
    acks: list[TaskAck],
) -> list[dict[str, Any]]:
    """把历史工单的「运行日志」从**已落库的记录**还原出来。

    ★ 为什么要做（2026-09-27 线上验收 P2-4）：
        这里过去写死 `"logs": []`，于是**任何**历史工单打开「运行日志」
        页签都是空的 —— 哪怕旁边「机器人报文」页签里正躺着 20 条真实轨迹点。
        看的人会以为「这功能没做」，而数据其实全在库里。

    ★ 只从已有列 / 已有行派生，不编造一条：
        - `t_task` 的生命周期时间戳列（assigned_at / started_at / finished_at）
        - `t_task_ack` 的 ACK 审计账本行（真实回执，带 command_id）
        - `t_track` 真实遥测的首 / 末点
      每一条都能追回具体的一列或一行，符合「宁可少，不造假」的口径。

    返回顺序为**时间正序**（前端 `logItems` 会自行 reverse 成最新在上）。
    """
    entries: list[tuple[datetime, str, str, dict[str, Any]]] = []

    # ---------- 0) 说明性首条：告诉阅读者这批日志的来源 ----------
    if task.created_at is not None:
        entries.append(
            (
                _aware(task.created_at),
                "system",
                "历史工单：以下日志由落库记录还原（非实时仿真会话）",
                {"source": "history", "task_id": task.task_id},
            )
        )

    # ---------- 1) 生命周期（全部来自 t_task 的真实时间戳列）----------
    if task.assigned_at is not None:
        entries.append(
            (
                _aware(task.assigned_at),
                "control",
                f"平台派单给机器人 {task.robot_id}" if task.robot_id else "平台建单，等待派单",
                {"robot_id": task.robot_id},
            )
        )
    if task.started_at is not None:
        entries.append(
            (
                _aware(task.started_at),
                "progress",
                "机器人抵达目标点，开始清理",
                {"robot_id": task.robot_id},
            )
        )
    if task.finished_at is not None:
        weight = task.collected_weight
        message = "作业完成"
        if weight is not None:
            message = f"作业完成，打捞 {float(weight):.3f} kg"
        entries.append(
            (
                _aware(task.finished_at),
                "done",
                message,
                {"collected_weight": float(weight) if weight is not None else None},
            )
        )

    # ---------- 2) ACK 审计账本（真实回执，带 command_id）----------
    for ack in acks:
        when = ack.received_wall_at or ack.received_at
        if when is None:
            continue
        entries.append(
            (
                _aware(when),
                "ack",
                f"机器人回执：{_ACK_OUTCOME_LABEL.get(ack.outcome, ack.outcome)}",
                {
                    "command_id": ack.command_id,
                    "device_id": ack.device_id,
                    "outcome": ack.outcome,
                    "reason": ack.reason,
                },
            )
        )

    # ---------- 3) 遥测首/末点（t_track 真实行）----------
    if trajectory:
        first, last = trajectory[0], trajectory[-1]
        if first.get("ts"):
            entries.append(
                (
                    _aware(datetime.fromisoformat(first["ts"])),
                    "telemetry",
                    f"遥测起点：电量 {first.get('battery')}%"
                    f"（{first['lng']:.5f},{first['lat']:.5f}）",
                    {"lng": first["lng"], "lat": first["lat"]},
                )
            )
        if last.get("ts"):
            bins = last.get("bins") or {}
            used = sum(float(v) for v in bins.values())
            entries.append(
                (
                    _aware(datetime.fromisoformat(last["ts"])),
                    "telemetry",
                    f"遥测终点：电量 {last.get('battery')}%，"
                    f"三仓占用合计 {used * 100:.1f}%（共 {len(trajectory)} 个轨迹点）",
                    {"lng": last["lng"], "lat": last["lat"], "points": len(trajectory)},
                )
            )

    entries.sort(key=lambda item: item[0])
    return [
        {
            "seq": index,
            "ts": when.isoformat(),
            "kind": kind,
            "message": message,
            "payload": payload,
        }
        for index, (when, kind, message, payload) in enumerate(entries, start=1)
    ]


def _track_out(track: Track, seq: int) -> dict[str, Any] | None:
    point = _parse_point(track.location)
    if point is None:
        return None
    lng, lat = point
    return {
        "seq": seq,
        "lng": lng,
        "lat": lat,
        "ts": track.recorded_at.isoformat() if track.recorded_at else None,
        "battery": track.battery,
        "bins": {
            "foam": float(track.bin_foam or 0),
            "plastic": float(track.bin_plastic or 0),
            "mixed": float(track.bin_mixed or 0),
        },
        "speed": float(track.speed) if track.speed is not None else None,
    }


async def _load_trajectory(session: AsyncSession, task_id: str) -> list[dict[str, Any]]:
    stmt = (
        select(Track)
        .where(Track.task_id == task_id)
        .order_by(Track.recorded_at, Track.id)
    )
    tracks = list((await session.execute(stmt)).scalars().all())
    items: list[dict[str, Any]] = []
    for track in tracks:
        item = _track_out(track, len(items) + 1)
        if item is not None:
            items.append(item)
    return items


async def _history_snapshot(
    task,
    trajectory: list[dict[str, Any]],
    acks: list[TaskAck] | None = None,
) -> dict[str, Any] | None:
    if not trajectory:
        return None

    first = trajectory[0]
    last = trajectory[-1]
    target = _parse_point(task.target_location)
    return {
        "task_id": task.task_id,
        "robot_id": task.robot_id,
        "event_id": task.event_id,
        "run_id": None,
        "command_id": None,
        "state": "history",
        "phase": "done" if task.status == "done" else task.status,
        "speed": 1,
        "position": {"lng": last["lng"], "lat": last["lat"]},
        "heading": 0,
        "home": {"lng": first["lng"], "lat": first["lat"]},
        "target": (
            {"lng": float(target[0]), "lat": float(target[1])}
            if target is not None
            else None
        ),
        "battery": last.get("battery"),
        "bins": last.get("bins") or {},
        "progress": 1 if task.status == "done" else 0,
        "route": [{"lng": p["lng"], "lat": p["lat"]} for p in trajectory],
        "remaining": [],
        "started_at": first.get("ts"),
        "updated_at": last.get("ts"),
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "error": None,
        # ★ 曾经写死 `[]`：历史工单的「运行日志」页签恒空，见 _history_logs 的说明
        "logs": _history_logs(task, trajectory, acks or []),
    }


@router.get("/{task_id}", response_model=ApiResponse[dict], summary="仿真运行状态")
async def get_simulation(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    task = await _authorized_task(session, task_id, user)
    snapshot = simulation_manager.snapshot(task_id)
    if snapshot is not None:
        return ApiResponse.ok(snapshot)

    trajectory = await _load_trajectory(session, task_id)
    acks = await _load_ack_rows(session, task_id)
    return ApiResponse.ok(await _history_snapshot(task, trajectory, acks))


@router.get(
    "/{task_id}/trajectory",
    response_model=ApiResponse[list],
    summary="任务轨迹（t_track）",
)
async def get_trajectory(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    await _authorized_task(session, task_id, user)
    return ApiResponse.ok(await _load_trajectory(session, task_id))


@router.post(
    "/{task_id}/start",
    response_model=ApiResponse[dict],
    summary="启动工单执行仿真",
)
async def start_simulation(
    task_id: str,
    payload: SimulationStartRequest | None = None,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    await _authorized_task(session, task_id, user)
    speed = _normalize_speed(payload.speed if payload is not None else 1.0)
    snapshot = await simulation_manager.start(task_id, speed=speed)
    return ApiResponse.ok(snapshot, message="仿真已启动")


@router.post(
    "/{task_id}/pause",
    response_model=ApiResponse[dict],
    summary="暂停仿真",
)
async def pause_simulation(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    await _authorized_task(session, task_id, user)
    return ApiResponse.ok(await simulation_manager.pause(task_id), message="仿真已暂停")


@router.post(
    "/{task_id}/resume",
    response_model=ApiResponse[dict],
    summary="继续仿真",
)
async def resume_simulation(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    await _authorized_task(session, task_id, user)
    return ApiResponse.ok(await simulation_manager.resume(task_id), message="仿真已继续")


@router.post(
    "/{task_id}/step",
    response_model=ApiResponse[dict],
    summary="仿真单步执行",
)
async def step_simulation(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    await _authorized_task(session, task_id, user)
    return ApiResponse.ok(await simulation_manager.step(task_id), message="已执行一步")


@router.post(
    "/{task_id}/speed",
    response_model=ApiResponse[dict],
    summary="调整仿真倍速",
)
async def set_simulation_speed(
    task_id: str,
    payload: SimulationSpeedRequest,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    await _authorized_task(session, task_id, user)
    speed = _normalize_speed(payload.speed)
    return ApiResponse.ok(
        await simulation_manager.set_speed(task_id, speed),
        message=f"仿真倍速已调整为 {speed:g}x",
    )


@router.post(
    "/{task_id}/stop",
    response_model=ApiResponse[dict],
    summary="停止仿真",
)
async def stop_simulation(
    task_id: str,
    payload: SimulationStopRequest | None = None,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    await _authorized_task(session, task_id, user)
    reason = (payload.reason if payload is not None else "").strip()
    return ApiResponse.ok(
        await simulation_manager.stop(task_id, reason=reason),
        message="仿真已停止",
    )
