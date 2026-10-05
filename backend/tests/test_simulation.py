"""工单执行仿真引擎单元测试。

这些测试不依赖 MQTT Broker 或真实数据库，只验证仿真运行标识、快照契约、
ACK 后处理顺序，以及路径/电量/仓容计算的边界。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.sim import (  # noqa: E402
    SimulationManager,
    SimulationPhase,
    SimulationRun,
    decay_battery,
    grow_bins,
    haversine_m,
    interpolate_polyline,
)


class _SessionContext:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> "_SessionContext":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def commit(self) -> None:
        self.commits += 1


def _make_run(**overrides) -> SimulationRun:
    values = {
        "task_id": "tsk_sim_001",
        "robot_id": "RBT-SIM-01",
        "event_id": "evt_sim_001",
        "main_class": "foam",
        "home": {"lng": 119.86, "lat": 26.35},
        "target": {"lng": 119.87, "lat": 26.36},
    }
    values.update(overrides)
    return SimulationRun(**values)


class TestSimulationRunContract:
    def test_generated_identifiers_are_unique(self) -> None:
        first = _make_run()
        second = _make_run()

        assert first.run_id != second.run_id
        assert first.command_id != second.command_id
        assert first.ack_id != second.ack_id
        assert first.command_id == f"cmd_{first.task_id}:sim_{first.run_id}"
        assert first.ack_id == f"ack_sim_{first.run_id}"

    def test_snapshot_exposes_frozen_contract(self) -> None:
        run = _make_run()
        snapshot = run.snapshot()

        assert {
            "task_id",
            "robot_id",
            "event_id",
            "run_id",
            "command_id",
            "state",
            "phase",
            "speed",
            "position",
            "heading",
            "home",
            "target",
            "battery",
            "bins",
            "progress",
            "route",
            "remaining",
            "started_at",
            "updated_at",
            "finished_at",
            "error",
            "logs",
        } <= snapshot.keys()
        assert snapshot["home"] == run.home
        assert snapshot["target"] == run.target
        assert snapshot["task_id"] == "tsk_sim_001"

    def test_send_ack_emits_ack_then_navigating_progress(self) -> None:
        manager = SimulationManager(tick_seconds=0.01, ack_seconds=0.0)
        run = _make_run()
        calls: list[tuple[str, dict]] = []

        async def handler(topic: str, payload: dict, **kwargs) -> None:
            calls.append((topic, payload))

        asyncio.run(
            manager._send_ack(
                run,
                handler,
                lambda: (lambda: None),
            )
        )

        assert [call[0] for call in calls] == [
            "robot/RBT-SIM-01/cmd/ack",
            "robot/RBT-SIM-01/task/progress",
        ]
        ack = calls[0][1]
        assert ack["ack_id"] == run.ack_id
        assert ack["command_id"] == run.command_id
        assert ack["task_id"] == run.task_id
        assert ack["device_id"] == run.robot_id
        assert ack["mode"] == "simulation"
        assert calls[1][1] == {
            "robot_id": run.robot_id,
            "task_id": run.task_id,
            "status": "navigating",
        }
        assert run.phase == SimulationPhase.NAVIGATING

    def test_stop_releases_robot_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = SimulationManager()
        run = _make_run()
        manager._runs[run.task_id] = run
        released: list[str] = []

        async def release(stopped: SimulationRun) -> None:
            released.append(stopped.run_id)

        monkeypatch.setattr(manager, "_release_robot", release)
        snapshot = asyncio.run(manager.stop(run.task_id, reason="测试停止"))

        assert snapshot["state"] == "stopped"
        assert released == [run.run_id]


class TestSimulationSessionFactory:
    def test_load_context_uses_nested_session_factory(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = _SessionContext()
        task = SimpleNamespace(
            task_id="tsk_sim_001",
            status="assigned",
            robot_id="RBT-SIM-01",
            event_id="evt_sim_001",
            target_location="POINT(119.87 26.36)",
        )
        robot = SimpleNamespace(
            location="POINT(119.86 26.35)",
            meta={"battery": 88, "bins": {"foam": 0.1}},
        )

        class TaskRepository:
            def __init__(self, injected_session: _SessionContext) -> None:
                assert injected_session is session

            async def get_by_task_id(self, task_id: str):
                assert task_id == task.task_id
                return task

        class DeviceRepository:
            def __init__(self, injected_session: _SessionContext) -> None:
                assert injected_session is session

            async def get_robot_by_id(self, robot_id: str):
                assert robot_id == task.robot_id
                return robot

        class EventRepository:
            def __init__(self, injected_session: _SessionContext) -> None:
                assert injected_session is session

            async def get_by_event_id(self, event_id: str):
                assert event_id == task.event_id
                return SimpleNamespace(main_class="foam")

        import app.repositories as repositories

        monkeypatch.setattr(repositories, "TaskRepository", TaskRepository)
        monkeypatch.setattr(repositories, "DeviceRepository", DeviceRepository)
        monkeypatch.setattr(repositories, "EventRepository", EventRepository)

        manager = SimulationManager(
            session_factory=lambda: (lambda: session),
        )
        context = asyncio.run(manager._load_context(task.task_id, "sim_run_001"))

        assert context.robot_id == task.robot_id
        assert context.main_class == "foam"
        assert context.home == {"lng": 119.86, "lat": 26.35}
        assert context.target == {"lng": 119.87, "lat": 26.36}
        assert robot.meta["simulated"] is True
        assert robot.meta["simulation_run_id"] == "sim_run_001"
        assert session.commits == 1

    def test_release_robot_uses_nested_session_factory(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = _SessionContext()
        run = _make_run()
        robot = SimpleNamespace(
            meta={
                "simulated": True,
                "current_task_id": run.task_id,
                "simulation_run_id": run.run_id,
                "battery": 88,
            }
        )

        class DeviceRepository:
            def __init__(self, injected_session: _SessionContext) -> None:
                assert injected_session is session

            async def get_robot_by_id(self, robot_id: str):
                assert robot_id == run.robot_id
                return robot

        import app.repositories as repositories

        monkeypatch.setattr(repositories, "DeviceRepository", DeviceRepository)

        manager = SimulationManager(
            session_factory=lambda: (lambda: session),
        )
        asyncio.run(manager._release_robot(run))

        assert robot.meta == {"battery": 88}
        assert session.commits == 1


class TestSimulationGeometry:
    def test_haversine_known_distances(self) -> None:
        origin = {"lng": 119.86, "lat": 26.35}
        assert haversine_m(origin, dict(origin)) == pytest.approx(0.0)
        assert haversine_m(
            {"lng": 0.0, "lat": 0.0},
            {"lng": 0.001, "lat": 0.0},
        ) == pytest.approx(111.195, rel=1e-3)
        assert haversine_m(
            {"lng": 0.0, "lat": 0.0},
            {"lng": 0.01, "lat": 0.0},
        ) == pytest.approx(
            haversine_m(
                {"lng": 0.01, "lat": 0.0},
                {"lng": 0.0, "lat": 0.0},
            )
        )

    def test_interpolate_polyline_endpoints_and_clamp(self) -> None:
        points = [
            {"lng": 0.0, "lat": 0.0},
            {"lng": 0.01, "lat": 0.0},
            {"lng": 0.02, "lat": 0.0},
        ]

        assert interpolate_polyline(points, -1.0) == (points[0], 0)
        point, segment = interpolate_polyline(points, 1.0)
        assert point == pytest.approx(points[-1])
        assert segment == 1
        assert interpolate_polyline(points, 2.0)[0] == pytest.approx(points[-1])
        midpoint, segment = interpolate_polyline(points, 0.5)
        assert midpoint["lng"] == pytest.approx(0.01)
        assert midpoint["lat"] == pytest.approx(0.0)
        assert segment == 0

    def test_interpolate_polyline_small_inputs(self) -> None:
        assert interpolate_polyline([], 0.5) == ({"lng": 0.0, "lat": 0.0}, 0)
        only = {"lng": 119.86, "lat": 26.35}
        assert interpolate_polyline([only], 0.5) == (only, 0)


class TestSimulationResources:
    def test_decay_battery_only_decreases_and_has_floor(self) -> None:
        assert decay_battery(92.0, 0.0) == pytest.approx(92.0)
        assert decay_battery(92.0, 10.0, factor=0.1) == pytest.approx(91.0)
        assert decay_battery(92.0, -1.0, factor=10.0) == pytest.approx(92.0)
        assert decay_battery(5.2, 1000.0, factor=1.0) == pytest.approx(5.0)

    def test_grow_bins_maps_class_and_clamps(self) -> None:
        initial = {"foam": 0.2, "plastic": 0.3, "mixed": 0.4}
        foam = grow_bins(initial, 10.0, "foam")
        assert foam["foam"] == pytest.approx(0.42)
        assert foam["plastic"] == pytest.approx(0.3)
        assert foam["mixed"] == pytest.approx(0.4)

        plastic = grow_bins(initial, 10.0, "plastic")
        assert plastic["plastic"] == pytest.approx(0.52)

        mixed = grow_bins(initial, 10.0, "fishing_gear")
        assert mixed["mixed"] == pytest.approx(0.62)

        capped = grow_bins({"foam": 0.99, "plastic": 0.0, "mixed": 0.0}, 100.0, "foam")
        assert capped["foam"] == pytest.approx(1.0)
        assert grow_bins(initial, -10.0, "foam") == pytest.approx(initial)
