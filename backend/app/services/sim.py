"""工单执行可视化仿真引擎。

MVP 不额外启动一台 MQTT Broker，也不另写一套任务状态机。仿真线程只负责
按时间生成设备侧报文，再把报文直接交给正式 MQTT handlers：

    ACK          -> handle_robot_ack
    telemetry    -> handle_telemetry
    progress     -> handle_robot_progress

因此状态迁移、ACK 审计、设备位置、``t_track`` 轨迹和 WebSocket 推送仍走
生产链路。当前边界仅是不经过 Broker 网络传输；将来替换为真实机器人时，
前端接口和页面不需要改。
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from loguru import logger

from app.core.exceptions import AppException, ConflictError, ErrorCode, NotFoundError
from app.models.task import TaskStatus
from app.services.dispatch import _parse_point


SimulationHandler = Callable[..., Awaitable[Any]]
SessionFactory = Callable[[], Any]


class SimulationState:
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    STOPPED = "stopped"
    ERROR = "error"

    ALL = (RUNNING, PAUSED, DONE, STOPPED, ERROR)


class SimulationPhase:
    ACK = "ack"
    NAVIGATING = "navigating"
    COLLECTING = "collecting"
    RETURNING = "returning"
    DONE = "done"


@dataclass(frozen=True)
class SimulationContext:
    """启动一次仿真所需的最小快照，生命周期结束后不再持有 ORM 对象。"""

    task_id: str
    robot_id: str
    event_id: str | None
    status: str
    battery: float
    bins: dict[str, float]
    home: dict[str, float]
    target: dict[str, float]
    main_class: str | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _valid_lnglat(lng: Any, lat: Any) -> tuple[float, float] | None:
    try:
        x = float(lng)
        y = float(lat)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    if not (-180 <= x <= 180 and -90 <= y <= 90):
        return None
    return x, y


def haversine_m(a: dict[str, float], b: dict[str, float]) -> float:
    """返回两个 WGS84 点之间的近似地面距离（米）。"""
    radius = 6_371_000.0
    lat1 = math.radians(float(a["lat"]))
    lat2 = math.radians(float(b["lat"]))
    dlat = lat2 - lat1
    dlng = math.radians(float(b["lng"]) - float(a["lng"]))
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


def heading_deg(a: dict[str, float], b: dict[str, float]) -> float:
    """返回从 a 指向 b 的方位角，正北为 0 度。"""
    lat1 = math.radians(float(a["lat"]))
    lat2 = math.radians(float(b["lat"]))
    dlng = math.radians(float(b["lng"]) - float(a["lng"]))
    y = math.sin(dlng) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def interpolate_polyline(
    points: list[dict[str, float]],
    fraction: float,
) -> tuple[dict[str, float], int]:
    """在线性折线上按累计距离插值，返回点位和所在段索引。"""
    if not points:
        return {"lng": 0.0, "lat": 0.0}, 0
    if len(points) == 1:
        return dict(points[0]), 0

    t = min(1.0, max(0.0, float(fraction)))
    lengths = [
        haversine_m(points[index], points[index + 1])
        for index in range(len(points) - 1)
    ]
    total = sum(lengths)
    if total <= 0:
        return dict(points[-1]), len(points) - 2

    remaining = total * t
    for index, length in enumerate(lengths):
        if remaining <= length or index == len(lengths) - 1:
            local = 1.0 if length <= 0 else min(1.0, remaining / length)
            start, end = points[index], points[index + 1]
            return {
                "lng": float(start["lng"]) + (float(end["lng"]) - float(start["lng"])) * local,
                "lat": float(start["lat"]) + (float(end["lat"]) - float(start["lat"])) * local,
            }, index
        remaining -= length

    return dict(points[-1]), len(points) - 2


def decay_battery(value: float, elapsed_seconds: float, factor: float = 0.08) -> float:
    """电量只减不增，最低保留 5%。"""
    return max(5.0, float(value) - max(0.0, elapsed_seconds) * factor)


def grow_bins(
    bins: dict[str, float],
    elapsed_seconds: float,
    main_class: str | None,
) -> dict[str, float]:
    """按垃圾类别增加对应仓容，所有值限制在 0~1。"""
    result = {
        "foam": max(0.0, min(1.0, float(bins.get("foam", 0.0)))),
        "plastic": max(0.0, min(1.0, float(bins.get("plastic", 0.0)))),
        "mixed": max(0.0, min(1.0, float(bins.get("mixed", 0.0)))),
    }
    key = {
        "foam": "foam",
        "plastic": "plastic",
        "fishing_gear": "mixed",
        "other": "mixed",
    }.get(main_class or "", "mixed")
    result[key] = min(1.0, result[key] + max(0.0, elapsed_seconds) * 0.022)
    return result


@dataclass
class SimulationRun:
    task_id: str
    robot_id: str
    event_id: str | None
    main_class: str | None
    home: dict[str, float]
    target: dict[str, float]
    state: str = SimulationState.RUNNING
    phase: str = SimulationPhase.ACK
    speed: float = 1.0
    position: dict[str, float] = field(default_factory=lambda: {"lng": 0.0, "lat": 0.0})
    heading: float = 0.0
    battery: float = 100.0
    bins: dict[str, float] = field(
        default_factory=lambda: {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
    )
    progress: float = 0.0
    route: list[dict[str, float]] = field(default_factory=list)
    remaining: list[dict[str, float]] = field(default_factory=list)
    nav_path: list[dict[str, float]] = field(default_factory=list)
    return_path: list[dict[str, float]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    started_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    finished_at: datetime | None = None
    error: str | None = None
    seq: int = 0
    _step_requests: int = 0
    _stop_requested: bool = False
    _pause_gate: asyncio.Event = field(default_factory=asyncio.Event)
    _wake_gate: asyncio.Event = field(default_factory=asyncio.Event)
    _handle: asyncio.Task[Any] | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    command_id: str = ""
    ack_id: str = ""

    def __post_init__(self) -> None:
        self.command_id = self.command_id or (
            f"cmd_{self.task_id}:sim_{self.run_id}"
        )
        self.ack_id = self.ack_id or f"ack_sim_{self.run_id}"
        self._pause_gate.set()
        self.position = dict(self.home)
        self.route = [dict(self.home)]

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "robot_id": self.robot_id,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "command_id": self.command_id,
            "state": self.state,
            "phase": self.phase,
            "speed": self.speed,
            "position": dict(self.position),
            "heading": round(float(self.heading), 1),
            "home": dict(self.home),
            "target": dict(self.target),
            "battery": int(round(self.battery)),
            "bins": {key: round(float(value), 4) for key, value in self.bins.items()},
            "progress": round(float(self.progress), 4),
            "route": [dict(point) for point in self.route],
            "remaining": [dict(point) for point in self.remaining],
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
            "logs": [dict(item) for item in self.logs[-240:]],
        }


class SimulationManager:
    """单进程仿真运行注册表。

    所有控制方法都在锁内改运行状态；仿真协程自身只读控制开关，不直接改任务
    状态。任务状态只由注入的正式 handlers 经过 ``DispatchEngine.transition``
    推进。
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        handlers: dict[str, SimulationHandler] | None = None,
        tick_seconds: float = 0.35,
        ack_seconds: float = 0.8,
        navigate_seconds: float = 14.0,
        collect_seconds: float = 8.0,
        return_seconds: float = 11.0,
    ) -> None:
        self._session_factory = session_factory
        self._handlers = handlers or {}
        self._tick_seconds = max(0.05, float(tick_seconds))
        self._ack_seconds = max(0.0, float(ack_seconds))
        self._navigate_seconds = max(0.0, float(navigate_seconds))
        self._collect_seconds = max(0.0, float(collect_seconds))
        self._return_seconds = max(0.0, float(return_seconds))
        self._runs: dict[str, SimulationRun] = {}
        self._lock = asyncio.Lock()

    def _factory(self) -> SessionFactory:
        if self._session_factory is not None:
            return self._session_factory
        from app.db.session import get_session_factory

        return get_session_factory

    def _handler(self, name: str, fallback: SimulationHandler) -> SimulationHandler:
        return self._handlers.get(name, fallback)

    async def _load_context(
        self,
        task_id: str,
        run_id: str,
    ) -> SimulationContext:
        from app.repositories import DeviceRepository, EventRepository, TaskRepository

        factory = self._factory()
        async with factory()() as session:
            task = await TaskRepository(session).get_by_task_id(task_id)
            if task is None:
                raise NotFoundError(f"任务 {task_id} 不存在", code=ErrorCode.TASK_NOT_FOUND)
            if task.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
                raise ConflictError(f"任务 {task_id} 已处于终态，不能再启动仿真")
            if not task.robot_id:
                raise ConflictError(f"任务 {task_id} 尚未绑定机器人，不能启动仿真")

            robot = await DeviceRepository(session).get_robot_by_id(task.robot_id)
            if robot is None:
                raise NotFoundError(f"机器人 {task.robot_id} 不存在", code=ErrorCode.DEVICE_NOT_FOUND)

            target = _parse_point(task.target_location)
            home = _parse_point(robot.location)
            if target is None:
                raise ConflictError(f"任务 {task_id} 的目标坐标无效")
            if home is None:
                raise ConflictError(f"机器人 {task.robot_id} 的当前位置无效")

            main_class = None
            if task.event_id:
                event = await EventRepository(session).get_by_event_id(task.event_id)
                main_class = event.main_class if event is not None else None

            meta = dict(robot.meta or {})
            meta["simulated"] = True
            meta["current_task_id"] = task_id
            meta["simulation_run_id"] = run_id
            meta["battery"] = int(meta.get("battery", 92))
            meta["bins"] = dict(meta.get("bins") or {})
            robot.meta = meta
            await session.commit()

            bins = meta.get("bins") or {}
            return SimulationContext(
                task_id=task.task_id,
                robot_id=task.robot_id,
                event_id=task.event_id,
                status=task.status,
                battery=float(meta.get("battery", 92)),
                bins={
                    key: float(bins.get(key, 0.0))
                    for key in ("foam", "plastic", "mixed")
                },
                home={"lng": float(home[0]), "lat": float(home[1])},
                target={"lng": float(target[0]), "lat": float(target[1])},
                main_class=main_class,
            )

    async def start(self, task_id: str, *, speed: float = 1.0) -> dict[str, Any]:
        async with self._lock:
            existing = self._runs.get(task_id)
            if existing is not None and existing.state in (
                SimulationState.RUNNING,
                SimulationState.PAUSED,
            ):
                return existing.snapshot()

            run_id = uuid.uuid4().hex[:12]
            context = await self._load_context(task_id, run_id)
            run = SimulationRun(
                task_id=task_id,
                robot_id=context.robot_id,
                event_id=context.event_id,
                main_class=context.main_class,
                home=context.home,
                target=context.target,
                run_id=run_id,
                speed=max(1.0, min(4.0, float(speed))),
                battery=context.battery,
                bins=dict(context.bins),
            )
            if context.status == TaskStatus.ASSIGNED:
                run.phase = SimulationPhase.ACK
            elif context.status == TaskStatus.NAVIGATING:
                run.phase = SimulationPhase.NAVIGATING
            elif context.status == TaskStatus.COLLECTING:
                run.phase = SimulationPhase.COLLECTING
            else:
                raise ConflictError(
                    f"任务 {task_id} 当前状态为 {context.status}，需先派单并绑定机器人"
                )

            run.nav_path = self._build_path(run.position, run.target)
            run.return_path = self._build_path(run.target, run.home)
            run.remaining = run.nav_path[1:] if run.phase != SimulationPhase.COLLECTING else []
            self._runs[task_id] = run
            self._log(run, "system", "仿真会话已创建，使用正式状态机与遥测 handler")
            run._handle = asyncio.create_task(self._run(run), name=f"simulation:{task_id}")
            return run.snapshot()

    async def pause(self, task_id: str) -> dict[str, Any]:
        run = self._require_run(task_id)
        async with self._lock:
            if run.state == SimulationState.RUNNING:
                run.state = SimulationState.PAUSED
                run._pause_gate.clear()
                run.updated_at = _utcnow()
                self._log(run, "control", "仿真已暂停")
            return run.snapshot()

    async def resume(self, task_id: str) -> dict[str, Any]:
        run = self._require_run(task_id)
        async with self._lock:
            if run.state == SimulationState.PAUSED:
                run.state = SimulationState.RUNNING
                run._pause_gate.set()
                run._wake_gate.set()
                run.updated_at = _utcnow()
                self._log(run, "control", "仿真继续执行")
            return run.snapshot()

    async def step(self, task_id: str) -> dict[str, Any]:
        run = self._require_run(task_id)
        async with self._lock:
            if run.state in (SimulationState.DONE, SimulationState.STOPPED, SimulationState.ERROR):
                return run.snapshot()
            run.state = SimulationState.PAUSED
            run._pause_gate.clear()
            run._step_requests += 1
            run._wake_gate.set()
            self._log(run, "control", "单步执行")
            return run.snapshot()

    async def set_speed(self, task_id: str, speed: float) -> dict[str, Any]:
        run = self._require_run(task_id)
        async with self._lock:
            run.speed = max(1.0, min(4.0, float(speed)))
            run.updated_at = _utcnow()
            self._log(run, "control", f"倍速调整为 {run.speed:g}x")
            run._wake_gate.set()
            return run.snapshot()

    async def stop(self, task_id: str, *, reason: str = "") -> dict[str, Any]:
        run = self._require_run(task_id)
        async with self._lock:
            if run.state in (SimulationState.DONE, SimulationState.STOPPED, SimulationState.ERROR):
                return run.snapshot()
            run._stop_requested = True
            run.state = SimulationState.STOPPED
            run._pause_gate.set()
            run._wake_gate.set()
            run.updated_at = _utcnow()
            run.finished_at = run.finished_at or _utcnow()
            self._log(run, "control", reason or "仿真已停止")
            snapshot = run.snapshot()
        await self._release_robot(run)
        return snapshot

    def snapshot(self, task_id: str) -> dict[str, Any] | None:
        run = self._runs.get(task_id)
        return run.snapshot() if run is not None else None

    async def wait(self, task_id: str) -> None:
        run = self._runs.get(task_id)
        if run is None or run._handle is None:
            return
        try:
            await run._handle
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def stop_all(self) -> None:
        runs = list(self._runs.values())
        handles: list[asyncio.Task[Any]] = []
        async with self._lock:
            for run in runs:
                if run.state in (SimulationState.RUNNING, SimulationState.PAUSED):
                    run._stop_requested = True
                    run.state = SimulationState.STOPPED
                    run.updated_at = _utcnow()
                    run._pause_gate.set()
                    run._wake_gate.set()
                if run._handle is not None:
                    handles.append(run._handle)
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)

    def _require_run(self, task_id: str) -> SimulationRun:
        run = self._runs.get(task_id)
        if run is None:
            raise NotFoundError(f"任务 {task_id} 没有正在执行的仿真会话")
        return run

    def _log(
        self,
        run: SimulationRun,
        kind: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        run.seq += 1
        run.logs.append(
            {
                "seq": run.seq,
                "ts": _utcnow().isoformat(),
                "kind": kind,
                "message": message,
                "payload": payload or {},
            }
        )
        if len(run.logs) > 240:
            run.logs = run.logs[-240:]

    @staticmethod
    def _build_path(start: dict[str, float], end: dict[str, float]) -> list[dict[str, float]]:
        distance = haversine_m(start, end)
        steps = max(12, min(80, int(distance / 90) + 12))
        return [
            {
                "lng": float(start["lng"]) + (float(end["lng"]) - float(start["lng"])) * index / steps,
                "lat": float(start["lat"]) + (float(end["lat"]) - float(start["lat"])) * index / steps,
            }
            for index in range(steps + 1)
        ]

    async def _run(self, run: SimulationRun) -> None:
        from app.mqtt.handlers import (
            handle_robot_ack,
            handle_robot_progress,
            handle_telemetry,
        )

        ack_handler = self._handler("ack", handle_robot_ack)
        telemetry_handler = self._handler("telemetry", handle_telemetry)
        progress_handler = self._handler("progress", handle_robot_progress)
        factory = self._factory()

        try:
            if run.phase == SimulationPhase.ACK:
                await self._send_ack(run, ack_handler, factory)
            if run.phase == SimulationPhase.NAVIGATING:
                await self._navigate(run, telemetry_handler, progress_handler, factory)
            if run.phase == SimulationPhase.COLLECTING:
                await self._collect(run, telemetry_handler, progress_handler, factory)
            if run.phase == SimulationPhase.RETURNING:
                await self._return(run, telemetry_handler, progress_handler, factory)
        except asyncio.CancelledError:
            run.state = SimulationState.STOPPED
            run.updated_at = _utcnow()
            run.finished_at = run.finished_at or _utcnow()
            raise
        except Exception as exc:  # noqa: BLE001
            run.state = SimulationState.ERROR
            run.error = f"{type(exc).__name__}: {exc}"
            run.updated_at = _utcnow()
            run.finished_at = _utcnow()
            self._log(run, "error", "仿真执行失败", {"error": run.error})
            logger.exception(f"[仿真] 任务 {run.task_id} 执行失败：{exc}")
        finally:
            await self._release_robot(run)

    async def _send_ack(
        self,
        run: SimulationRun,
        handler: SimulationHandler,
        factory: SessionFactory,
    ) -> None:
        self._log(run, "control", "平台下发任务指令", {"command_id": run.command_id})
        await self._tick_sleep(run, self._ack_seconds)
        if run._stop_requested:
            return
        payload = {
            "ack_id": run.ack_id,
            "task_id": run.task_id,
            "command_id": run.command_id,
            "device_id": run.robot_id,
            "seq": 0,
            "received_at": time.time(),
            "accepted": True,
            "reason": "",
            "mode": "simulation",
        }
        self._log(run, "ack", "机器人确认接单", payload)
        await handler(
            f"robot/{run.robot_id}/cmd/ack",
            payload,
            session_factory=factory,
        )
        await handler(
            f"robot/{run.robot_id}/task/progress",
            {"robot_id": run.robot_id, "task_id": run.task_id, "status": "navigating"},
            session_factory=factory,
        )
        run.phase = SimulationPhase.NAVIGATING

    async def _navigate(
        self,
        run: SimulationRun,
        telemetry_handler: SimulationHandler,
        progress_handler: SimulationHandler,
        factory: SessionFactory,
    ) -> None:
        duration = self._navigate_seconds
        elapsed = 0.0
        path = run.nav_path or self._build_path(run.position, run.target)
        while elapsed < duration and not run._stop_requested:
            await self._wait_for_turn(run)
            if run._stop_requested:
                return
            dt = self._tick_seconds * run.speed
            elapsed += dt
            fraction = min(1.0, elapsed / duration)
            previous = dict(run.position)
            run.position, segment = interpolate_polyline(path, fraction)
            run.heading = heading_deg(previous, run.position)
            run.remaining = path[min(segment + 2, len(path)) :]
            self._append_route(run)
            run.progress = fraction
            run.battery = decay_battery(run.battery, dt, 0.06)
            await self._send_telemetry(run, telemetry_handler, factory, "navigating")
            await asyncio.sleep(self._tick_seconds)

        if run._stop_requested:
            return
        run.position = dict(run.target)
        run.remaining = []
        run.progress = 1.0
        self._append_route(run)
        await progress_handler(
            f"robot/{run.robot_id}/task/progress",
            {"robot_id": run.robot_id, "task_id": run.task_id, "status": "collecting"},
            session_factory=factory,
        )
        self._log(run, "progress", "机器人抵达目标点，开始清理")
        run.phase = SimulationPhase.COLLECTING
        await self._collect(run, telemetry_handler, progress_handler, factory)

    async def _collect(
        self,
        run: SimulationRun,
        telemetry_handler: SimulationHandler,
        progress_handler: SimulationHandler,
        factory: SessionFactory,
    ) -> None:
        duration = self._collect_seconds
        elapsed = 0.0
        run.phase = SimulationPhase.COLLECTING
        while elapsed < duration and not run._stop_requested:
            await self._wait_for_turn(run)
            if run._stop_requested:
                return
            dt = self._tick_seconds * run.speed
            elapsed += dt
            run.bins = grow_bins(run.bins, dt, run.main_class)
            run.battery = decay_battery(run.battery, dt, 0.035)
            run.progress = min(1.0, elapsed / duration)
            await self._send_telemetry(run, telemetry_handler, factory, "collecting")
            await asyncio.sleep(self._tick_seconds)

        if run._stop_requested:
            return
        await progress_handler(
            f"robot/{run.robot_id}/task/progress",
            {"robot_id": run.robot_id, "task_id": run.task_id, "status": "returning"},
            session_factory=factory,
        )
        self._log(run, "progress", "清理完成，机器人开始返航")
        run.phase = SimulationPhase.RETURNING
        await self._return(run, telemetry_handler, progress_handler, factory)

    async def _return(
        self,
        run: SimulationRun,
        telemetry_handler: SimulationHandler,
        progress_handler: SimulationHandler,
        factory: SessionFactory,
    ) -> None:
        duration = self._return_seconds
        elapsed = 0.0
        path = run.return_path or self._build_path(run.position, run.home)
        run.phase = SimulationPhase.RETURNING
        while elapsed < duration and not run._stop_requested:
            await self._wait_for_turn(run)
            if run._stop_requested:
                return
            dt = self._tick_seconds * run.speed
            elapsed += dt
            fraction = min(1.0, elapsed / duration)
            previous = dict(run.position)
            run.position, segment = interpolate_polyline(path, fraction)
            run.heading = heading_deg(previous, run.position)
            run.remaining = path[min(segment + 2, len(path)) :]
            self._append_route(run)
            run.battery = decay_battery(run.battery, dt, 0.05)
            await self._send_telemetry(run, telemetry_handler, factory, "returning")
            await asyncio.sleep(self._tick_seconds)

        if run._stop_requested:
            return
        run.position = dict(run.home)
        run.remaining = []
        self._append_route(run)
        await self._send_telemetry(run, telemetry_handler, factory, "idle")
        payload = {
            "robot_id": run.robot_id,
            "task_id": run.task_id,
            "status": "done",
            "collected_weight": 8.5,
            "review_result": "confirmed",
            "evidence_url": f"simulation://{run.task_id}",
        }
        await progress_handler(
            f"robot/{run.robot_id}/task/progress",
            payload,
            session_factory=factory,
        )
        await self._release_robot(run)
        run.phase = SimulationPhase.DONE
        run.state = SimulationState.DONE
        run.progress = 1.0
        run.updated_at = _utcnow()
        run.finished_at = _utcnow()
        self._log(run, "done", "工单闭环完成，事件已同步为已解决")

    async def _send_telemetry(
        self,
        run: SimulationRun,
        handler: SimulationHandler,
        factory: SessionFactory,
        status: str,
    ) -> None:
        payload = {
            "device_id": run.robot_id,
            "device_type": "robot",
            "task_id": run.task_id,
            "location": {"lng": run.position["lng"], "lat": run.position["lat"]},
            "battery": int(round(run.battery)),
            "bins": {key: round(float(value), 4) for key, value in run.bins.items()},
            "speed": 1.2 if status in ("navigating", "returning") else 0.2,
            "status": status,
            "heading": round(float(run.heading), 1),
        }
        self._log(run, "telemetry", f"{status} 遥测上报", payload)
        run.updated_at = _utcnow()
        await handler(
            f"marine/lianjiang/{run.robot_id}/telemetry",
            payload,
            session_factory=factory,
        )

    async def _release_robot(self, run: SimulationRun) -> None:
        from app.repositories import DeviceRepository

        try:
            factory = self._factory()
            async with factory()() as session:
                robot = await DeviceRepository(session).get_robot_by_id(run.robot_id)
                if robot is not None:
                    meta = dict(robot.meta or {})
                    if (
                        meta.get("current_task_id") == run.task_id
                        and meta.get("simulation_run_id") == run.run_id
                    ):
                        meta.pop("current_task_id", None)
                        meta.pop("simulation_run_id", None)
                        meta.pop("simulated", None)
                    robot.meta = meta
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[仿真] 释放机器人 {run.robot_id} 失败：{exc}")

    async def _wait_for_turn(self, run: SimulationRun) -> None:
        while not run._pause_gate.is_set() and not run._stop_requested:
            if run._step_requests > 0:
                run._step_requests -= 1
                return
            run._wake_gate.clear()
            await run._wake_gate.wait()

    async def _tick_sleep(self, run: SimulationRun, seconds: float) -> None:
        remaining = max(0.0, seconds / max(1.0, run.speed))
        while remaining > 0 and not run._stop_requested:
            await self._wait_for_turn(run)
            if run._stop_requested:
                return
            slice_size = min(self._tick_seconds, remaining)
            await asyncio.sleep(slice_size)
            remaining -= slice_size

    @staticmethod
    def _append_route(run: SimulationRun) -> None:
        point = dict(run.position)
        if not run.route or haversine_m(run.route[-1], point) >= 0.5:
            run.route.append(point)
            if len(run.route) > 1200:
                run.route = run.route[-1200:]


simulation_manager = SimulationManager()
