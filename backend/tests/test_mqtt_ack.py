"""WP-14C ACK 接收、校验与幂等 —— 平台侧测试。

覆盖执行手册「最低测试矩阵」全部八项：

1. 冻结 ACK 无 `task_id` 时能反推并推进任务；
2. 旧 ACK 字段仍兼容；
3. `accepted=false` 首次回退、重复不重复回退；
4. `command_id`、设备 ID、topic 不一致时全部拒绝；
5. duplicate / late / out_of_order / new 四类判定与优先级；
6. QoS1 重复 ACK 不重复推进；
7. 未登记 deadline 的命令仍可接收，但不得伪造 `late` 结论；

外加边界：malformed payload、未知 task、未知 command、seq 类型错误、
received_at 类型错误、异常隔离（不打断 MQTT 主循环）。

全部为纯内存实现：不依赖真实 MQTT / 网络 / 数据库。
- 解析与 AckTracker 直接单元测试；
- `handle_robot_ack` 注入 fake session/repository/engine + 独立 AckTracker。
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

from app.mqtt.ack import (  # noqa: E402
    AckResult,
    AckTracker,
    AckEnvelope,
    parse_ack_envelope,
    parse_received_at,
    task_id_from_command_id,
)


# ======================================================================
# 入站解析：冻结信封优先，旧字段兼容，task_id 反推
# ======================================================================
class TestAckEnvelopeParsing:
    """`parse_ack_envelope` 必须优先冻结信封并兼容旧字段。"""

    def test_frozen_envelope_derives_task_id(self) -> None:
        """★ 矩阵第 1 项：冻结 ACK 没有顶层 task_id，必须从 command_id 反推。"""
        env = parse_ack_envelope(
            {
                "ack_id": "ack_0001",
                "command_id": "cmd_tsk_frozen_001",
                "device_id": "RBT-001",
                "seq": 1,
                "received_at": 100.0,
                "accepted": True,
                "reason": "dispatched",
                "mode": "navigating",
            }
        )
        assert env is not None
        assert env.task_id == "tsk_frozen_001"
        assert env.command_id == "cmd_tsk_frozen_001"
        assert env.device_id == "RBT-001"
        assert env.seq == 1
        assert env.received_at == 100.0
        assert env.accepted is True
        assert env.reason == "dispatched"
        assert env.mode == "navigating"
        assert env.ack_id == "ack_0001"
        assert env.legacy is False

    def test_legacy_envelope_still_compatible(self) -> None:
        """★ 矩阵第 2 项：旧字段 task_id/robot_id/ts 仍兼容，且派生去重键。"""
        env = parse_ack_envelope(
            {
                "task_id": "tsk_legacy_001",
                "robot_id": "RBT-001",
                "accepted": True,
                "ts": "2026-09-18T01:23:48+08:00",
            }
        )
        assert env is not None
        assert env.task_id == "tsk_legacy_001"
        assert env.command_id == "cmd_tsk_legacy_001"   # 派生，与 command_id_for_task 同构
        assert env.device_id == "RBT-001"
        assert env.accepted is True
        assert env.seq == 0                              # 旧格式无 seq，兜底 0
        assert env.legacy is True

    def test_legacy_ts_epoch_and_iso_parse(self) -> None:
        """旧字段 ts 支持 epoch 秒与 ISO-8601 两种形态。"""
        from datetime import datetime, timezone

        epoch = parse_ack_envelope(
            {"task_id": "tsk_t1", "robot_id": "RBT-1", "accepted": True, "ts": 1758230401.0}
        )
        iso = parse_ack_envelope(
            {"task_id": "tsk_t2", "robot_id": "RBT-1", "accepted": True, "ts": "2026-09-18T01:23:48Z"}
        )
        expected = datetime(2026, 9, 18, 1, 23, 48, tzinfo=timezone.utc).timestamp()
        assert epoch is not None and epoch.received_at == 1758230401.0
        assert iso is not None and iso.received_at == pytest.approx(expected)

    def test_task_id_and_command_id_consistent_ok(self) -> None:
        """顶层 task_id 与 command_id=cmd_{task_id} 一致 → 放行。"""
        env = parse_ack_envelope(
            {
                "task_id": "tsk_both_001",
                "command_id": "cmd_tsk_both_001",
                "device_id": "RBT-001",
                "seq": 1,
                "received_at": 1.0,
                "accepted": True,
            }
        )
        assert env is not None
        assert env.task_id == "tsk_both_001"

    def test_simulation_command_extension_accepted(self) -> None:
        """仿真模式允许 cmd_{task_id}:sim_{run_id}，并解析回真实 task_id。"""
        env = parse_ack_envelope(
            {
                "ack_id": "ack_sim_run_001",
                "task_id": "tsk_sim_001",
                "command_id": "cmd_tsk_sim_001:sim_run_001",
                "device_id": "RBT-SIM-01",
                "seq": 0,
                "received_at": 100.0,
                "accepted": True,
                "mode": "simulation",
            }
        )
        assert env is not None
        assert env.task_id == "tsk_sim_001"
        assert env.command_id == "cmd_tsk_sim_001:sim_run_001"
        assert env.mode == "simulation"

    def test_simulation_command_requires_simulation_mode(self) -> None:
        """标准模式不得借仿真扩展绕过 cmd_{task_id} 契约。"""
        env = parse_ack_envelope(
            {
                "task_id": "tsk_sim_001",
                "command_id": "cmd_tsk_sim_001:sim_run_001",
                "device_id": "RBT-SIM-01",
                "seq": 0,
                "received_at": 100.0,
                "accepted": True,
                "mode": "navigating",
            }
        )
        assert env is None

    def test_simulation_command_requires_top_task_id(self) -> None:
        """仿真扩展必须携带顶层 task_id，不能仅凭 command_id 猜测任务。"""
        env = parse_ack_envelope(
            {
                "command_id": "cmd_tsk_sim_001:sim_run_001",
                "device_id": "RBT-SIM-01",
                "seq": 0,
                "received_at": 100.0,
                "accepted": True,
                "mode": "simulation",
            }
        )
        assert env is None

    def test_simulation_command_task_mismatch_rejected(self) -> None:
        """顶层 task_id 与仿真 command_id 指向不同任务时整条拒绝。"""
        env = parse_ack_envelope(
            {
                "task_id": "tsk_other",
                "command_id": "cmd_tsk_sim_001:sim_run_001",
                "device_id": "RBT-SIM-01",
                "seq": 0,
                "received_at": 100.0,
                "accepted": True,
                "mode": "simulation",
            }
        )
        assert env is None

    def test_task_id_command_id_conflict_rejected(self) -> None:
        """★ 矩阵第 4 项：顶层 task_id 与 command_id 不一致 → 整条拒绝。"""
        env = parse_ack_envelope(
            {
                "task_id": "tsk_a",
                "command_id": "cmd_tsk_b",
                "device_id": "RBT-1",
                "seq": 1,
                "received_at": 1.0,
                "accepted": True,
            }
        )
        assert env is None

    def test_unknown_command_shape_rejected(self) -> None:
        """command_id 非 cmd_{task_id} 形态且无顶层 task_id → 无法反推，拒绝。"""
        env = parse_ack_envelope(
            {"command_id": "CMD-123", "device_id": "RBT-1", "seq": 1,
             "received_at": 1.0, "accepted": True}
        )
        assert env is None

    def test_neither_command_nor_task_id_rejected(self) -> None:
        env = parse_ack_envelope(
            {"device_id": "RBT-1", "seq": 1, "received_at": 1.0, "accepted": True}
        )
        assert env is None

    def test_missing_device_and_robot_id_rejected(self) -> None:
        env = parse_ack_envelope(
            {"command_id": "cmd_t1", "seq": 1, "received_at": 1.0, "accepted": True}
        )
        assert env is None

    def test_device_robot_conflict_frozen_wins(self) -> None:
        """冻结 device_id 与旧 robot_id 冲突时冻结字段优先。"""
        env = parse_ack_envelope(
            {
                "command_id": "cmd_t1",
                "device_id": "RBT-001",
                "robot_id": "RBT-999",
                "seq": 1,
                "received_at": 1.0,
                "accepted": True,
            }
        )
        assert env is not None
        assert env.device_id == "RBT-001"

    @pytest.mark.parametrize("bad", ["abc", "-1", 1.5, True, None, [], {}])
    def test_seq_type_errors_rejected(self, bad) -> None:
        """seq 字段存在但类型错误 → 丢弃（不得放行、不得抛异常）。"""
        env = parse_ack_envelope(
            {"command_id": "cmd_t1", "device_id": "RBT-1", "seq": bad,
             "received_at": 1.0, "accepted": True}
        )
        assert env is None

    @pytest.mark.parametrize("good", [0, 5, "7", 9.0])
    def test_seq_valid_forms_accepted(self, good) -> None:
        env = parse_ack_envelope(
            {"command_id": "cmd_t1", "device_id": "RBT-1", "seq": good,
             "received_at": 1.0, "accepted": True}
        )
        assert env is not None
        assert env.seq == int(good)

    @pytest.mark.parametrize("bad", ["nope", None, "", True, [], {}])
    def test_received_at_type_errors_rejected(self, bad) -> None:
        """received_at（旧 ts）不可解析 → 丢弃。"""
        env = parse_ack_envelope(
            {"command_id": "cmd_t1", "device_id": "RBT-1", "seq": 1,
             "received_at": bad, "accepted": True}
        )
        assert env is None

    @pytest.mark.parametrize("bad", ["yes", 2, None, [], {}])
    def test_accepted_type_errors_rejected(self, bad) -> None:
        env = parse_ack_envelope(
            {"command_id": "cmd_t1", "device_id": "RBT-1", "seq": 1,
             "received_at": 1.0, "accepted": bad}
        )
        assert env is None

    @pytest.mark.parametrize("value,expected", [(True, True), (False, False),
                                                ("true", True), ("false", False),
                                                (1, True), (0, False), ("1", True), ("0", False)])
    def test_accepted_forms(self, value, expected) -> None:
        env = parse_ack_envelope(
            {"command_id": "cmd_t1", "device_id": "RBT-1", "seq": 1,
             "received_at": 1.0, "accepted": value}
        )
        assert env is not None
        assert env.accepted is expected

    @pytest.mark.parametrize("bad", [None, "x", [1, 2], 42])
    def test_malformed_payload_not_dict_rejected(self, bad) -> None:
        """malformed payload → 丢弃，不抛异常。"""
        assert parse_ack_envelope(bad) is None

    def test_task_id_from_command_id(self) -> None:
        assert task_id_from_command_id("cmd_tsk_x") == "tsk_x"
        assert task_id_from_command_id("cmd_") is None
        assert task_id_from_command_id("CMD-123") is None
        assert task_id_from_command_id("x") is None

    def test_parse_received_at_forms(self) -> None:
        from datetime import datetime, timezone

        assert parse_received_at(100.0) == 100.0
        assert parse_received_at(100) == 100.0
        assert parse_received_at("100.5") == 100.5
        assert parse_received_at("2026-09-18T01:23:48+08:00") == pytest.approx(
            datetime(2026, 9, 17, 17, 23, 48, tzinfo=timezone.utc).timestamp()
        )
        assert parse_received_at("2026-09-18T01:23:48Z") == pytest.approx(
            datetime(2026, 9, 18, 1, 23, 48, tzinfo=timezone.utc).timestamp()
        )
        assert parse_received_at("2026-09-18T01:23:48") is not None
        dt = datetime(2026, 9, 18, 1, 23, 48, tzinfo=timezone.utc)
        assert parse_received_at(dt) == pytest.approx(dt.timestamp())
        # 注入的接收时间可确定解析，不依赖真实时钟（无 datetime.now 兜底）
        assert parse_received_at("not-a-date") is None


# ======================================================================
# AckTracker：duplicate > late > out_of_order > new
# ======================================================================
class TestAckTrackerClassification:
    """★ 矩阵第 5 项：四类判定与优先级。"""

    def _env(self, command_id: str, device_id: str = "RBT-001", seq: int = 1,
             received_at: float = 100.0, accepted: bool = True, task_id: str | None = None) -> AckEnvelope:
        return AckEnvelope(
            command_id=command_id,
            task_id=task_id or command_id[len("cmd_"):],
            device_id=device_id,
            seq=seq,
            received_at=received_at,
            accepted=accepted,
            reason="",
            mode="",
        )

    def test_new_first_in_order(self) -> None:
        tracker = AckTracker()
        assert tracker.classify(self._env("cmd_t1")) == AckResult.NEW
        assert tracker.record(self._env("cmd_t1")) == AckResult.NEW
        assert tracker.is_acked("cmd_t1")
        assert tracker.counts[AckResult.NEW] == 1

    def test_duplicate_returns_first_canonical(self) -> None:
        tracker = AckTracker()
        first = self._env("cmd_t1", seq=1, received_at=100.0, accepted=True)
        assert tracker.record(first) == AckResult.NEW
        dup = self._env("cmd_t1", seq=1, received_at=100.0, accepted=True)
        assert tracker.record(dup) == AckResult.DUPLICATE
        stored = tracker.ack_for("cmd_t1")
        assert stored is not None
        assert stored.received_at == 100.0
        assert stored.accepted is True
        assert tracker.counts[AckResult.DUPLICATE] == 1

    def test_late_when_received_after_deadline(self) -> None:
        tracker = AckTracker()
        tracker.register_command("cmd_t1", "RBT-001", 1, expires_at=100.0)
        assert tracker.classify(self._env("cmd_t1", received_at=200.0)) == AckResult.LATE
        assert tracker.record(self._env("cmd_t1", received_at=200.0)) == AckResult.LATE
        assert tracker.is_acked("cmd_t1")          # late 仍记录

    def test_late_not_within_deadline(self) -> None:
        tracker = AckTracker()
        tracker.register_command("cmd_t1", "RBT-001", 1, expires_at=100.0)
        assert tracker.record(self._env("cmd_t1", received_at=99.0)) == AckResult.NEW

    def test_unregistered_command_not_faked_late(self) -> None:
        """★ 矩阵第 7 项：未登记 deadline 的命令不得伪判 late。"""
        tracker = AckTracker()
        env = self._env("cmd_unregistered", received_at=1e12)
        assert tracker.has_deadline("cmd_unregistered") is False
        assert tracker.record(env) == AckResult.NEW        # 不是 late
        assert tracker.record(env) == AckResult.DUPLICATE  # 第二次自然判重

    def test_out_of_order_when_seq_below_watermark(self) -> None:
        tracker = AckTracker()
        assert tracker.record(self._env("cmd_a", seq=5)) == AckResult.NEW      # 水位 5
        assert tracker.record(self._env("cmd_b", seq=3)) == AckResult.OUT_OF_ORDER
        assert tracker.is_acked("cmd_b")           # out_of_order 仍记录
        assert tracker.counts[AckResult.OUT_OF_ORDER] == 1

    def test_priority_duplicate_over_late(self) -> None:
        """★ 优先级：同 command_id 已登记 → duplicate，即使满足 late 条件。"""
        tracker = AckTracker()
        tracker.register_command("cmd_t1", "RBT-001", 1, expires_at=100.0)
        assert tracker.record(self._env("cmd_t1", received_at=50.0)) == AckResult.NEW
        assert tracker.record(self._env("cmd_t1", received_at=200.0)) == AckResult.DUPLICATE

    def test_priority_duplicate_over_out_of_order(self) -> None:
        tracker = AckTracker()
        assert tracker.record(self._env("cmd_a", seq=5)) == AckResult.NEW
        # 同一 command_id 的乱序重复 → duplicate（不是 out_of_order）
        assert tracker.record(self._env("cmd_a", seq=1)) == AckResult.DUPLICATE

    def test_watermark_per_device_independent(self) -> None:
        tracker = AckTracker()
        assert tracker.record(self._env("cmd_a", device_id="RBT-001", seq=5)) == AckResult.NEW
        assert tracker.record(self._env("cmd_b", device_id="RBT-002", seq=1)) == AckResult.NEW
        assert tracker.record(self._env("cmd_c", device_id="RBT-002", seq=2)) == AckResult.NEW
        # RBT-001 的水位仍为 5，seq=3 对它算乱序
        assert tracker.record(self._env("cmd_d", device_id="RBT-001", seq=3)) == AckResult.OUT_OF_ORDER

    def test_classify_does_not_store(self) -> None:
        tracker = AckTracker()
        assert tracker.classify(self._env("cmd_t1")) == AckResult.NEW
        assert tracker.is_acked("cmd_t1") is False     # classify 是纯判定
        assert tracker.record(self._env("cmd_t1")) == AckResult.NEW

    def test_record_idempotent_keeps_first(self) -> None:
        tracker = AckTracker()
        tracker.record(self._env("cmd_t1", received_at=10.0, accepted=False))
        tracker.record(self._env("cmd_t1", received_at=20.0, accepted=True))
        stored = tracker.ack_for("cmd_t1")
        assert stored is not None
        assert stored.received_at == 10.0              # 首次规范回执不被改写
        assert stored.accepted is False
        assert tracker.counts[AckResult.NEW] == 1
        assert tracker.counts[AckResult.DUPLICATE] == 1


# ======================================================================
# handler 接线：冻结 / 旧字段 / 回退 / 身份校验 / QoS1 幂等
# ======================================================================
class FakeTask:
    """最小任务替身（只含 handler 需要的字段）。"""

    def __init__(self, task_id: str, robot_id: str | None = "RBT-001",
                 status: str = "assigned") -> None:
        self.task_id = task_id
        self.robot_id = robot_id
        self.status = status
        self.remark = None


def make_repo_class(task: FakeTask):
    """构造闭包当前任务的假 TaskRepository（handler 每次调用都会新建实例）。"""
    class _TaskRepository:
        def __init__(self, session=None) -> None:
            self.session = session

        async def get_by_task_id(self, task_id: str) -> FakeTask | None:
            return task if task.task_id == task_id else None

    return _TaskRepository


class FakeDispatchEngine:
    """记录 transition 调用，模拟状态迁移。"""

    def __init__(self, session) -> None:
        self.session = session
        self.transitions: list[dict] = []

    async def transition(self, task: FakeTask, target_status: str, **kwargs) -> FakeTask:
        self.transitions.append(
            {"task_id": task.task_id, "target": target_status, "kwargs": kwargs}
        )
        task.status = target_status
        if "remark" in kwargs:
            task.remark = kwargs["remark"]
        return task


class FakeSession:
    def __init__(self) -> None:
        self.committed = 0

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def commit(self) -> None:
        self.committed += 1


def make_session_factory(session: FakeSession):
    """模拟 `get_session_factory()()` 的双重调用形态：工厂 → maker → session。"""
    return lambda: (lambda: session)


@pytest.fixture
def ack_deps(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """注入 fake repository / engine + 独立 AckTracker + fake session。

    ★ 必须 patch `app.mqtt.handlers.DispatchEngine`（handler 用的是模块级
    import 的绑定名，而不是每次重查 `app.services.dispatch.DispatchEngine`）
    —— patch 后者只在 handlers 首次被 import 的瞬间生效，之后会静默
    指向上一个测试的 engine。
    """
    import app.mqtt.handlers as handlers_mod
    import app.repositories as repositories_mod

    task = FakeTask(task_id="tsk_frozen_001", robot_id="RBT-001", status="assigned")
    session = FakeSession()
    engine = FakeDispatchEngine(session)
    tracker = AckTracker()

    monkeypatch.setattr(repositories_mod, "TaskRepository", make_repo_class(task))
    monkeypatch.setattr(handlers_mod, "DispatchEngine", lambda s: engine)

    return SimpleNamespace(
        task=task,
        session=session,
        engine=engine,
        tracker=tracker,
        session_factory=make_session_factory(session),
        topic="robot/RBT-001/cmd/ack",
    )


def run_ack(ack_deps: SimpleNamespace, payload: dict) -> str | None:
    from app.mqtt.handlers import handle_robot_ack

    return asyncio.run(
        handle_robot_ack(
            ack_deps.topic,
            payload,
            tracker=ack_deps.tracker,
            session_factory=ack_deps.session_factory,
        )
    )


class TestHandleRobotAck:
    """handler 与任务状态机的接线（WP-14C 条款 6~7）。"""

    def test_frozen_ack_derives_task_and_advances(self, ack_deps: SimpleNamespace) -> None:
        """★ 矩阵第 1 项：冻结 ACK 无 task_id → 反推 → assigned → navigating。"""
        payload = {
            "ack_id": "ack_0001",
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
            "reason": "dispatched",
            "mode": "navigating",
        }
        outcome = run_ack(ack_deps, payload)
        assert outcome == AckResult.NEW
        assert ack_deps.task.status == "navigating"
        assert ack_deps.engine.transitions[-1]["target"] == "navigating"
        assert ack_deps.tracker.is_acked("cmd_tsk_frozen_001")
        assert ack_deps.session.committed >= 1

    def test_legacy_ack_still_advances(self, ack_deps: SimpleNamespace) -> None:
        """★ 矩阵第 2 项：旧字段 ACK（task_id/robot_id/ts）仍能推进。"""
        ack_deps.task.task_id = "tsk_legacy_001"
        payload = {
            "task_id": "tsk_legacy_001",
            "robot_id": "RBT-001",
            "accepted": True,
            "ts": "2026-09-18T01:23:48+08:00",
        }
        outcome = run_ack(ack_deps, payload)
        assert outcome == AckResult.NEW
        assert ack_deps.task.status == "navigating"

    def test_accept_false_first_reverts(self, ack_deps: SimpleNamespace) -> None:
        """★ 矩阵第 3 项：accepted=false 首次回退：pending + 清空 robot_id + 记录原因。"""
        ack_deps.task.task_id = "tsk_rej_001"
        payload = {
            "command_id": "cmd_tsk_rej_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": False,
            "reason": "busy",
        }
        outcome = run_ack(ack_deps, payload)
        assert outcome == AckResult.NEW
        assert ack_deps.task.status == "pending"
        assert ack_deps.task.robot_id is None
        assert ack_deps.task.remark == "设备拒绝：busy"
        assert ack_deps.engine.transitions[-1]["target"] == "pending"

    def test_accept_false_duplicate_no_re_revert(self, ack_deps: SimpleNamespace) -> None:
        """★ 矩阵第 3 项：重复拒绝回执不重复回退（同 command_id → duplicate）。"""
        ack_deps.task.task_id = "tsk_rej_002"
        payload = {
            "command_id": "cmd_tsk_rej_002",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": False,
            "reason": "busy",
        }
        assert run_ack(ack_deps, payload) == AckResult.NEW
        assert ack_deps.task.status == "pending"
        second = run_ack(ack_deps, payload)
        assert second == AckResult.DUPLICATE
        assert ack_deps.task.status == "pending"          # 不再回退
        assert len(ack_deps.engine.transitions) == 1       # 状态机只动过一次

    def test_accept_true_not_assigned_no_advance(self, ack_deps: SimpleNamespace) -> None:
        """accepted=true 但任务已不在 assigned → 不重复推进（仍记录回执）。"""
        ack_deps.task.status = "navigating"
        payload = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
        }
        outcome = run_ack(ack_deps, payload)
        assert outcome == AckResult.NEW
        assert ack_deps.task.status == "navigating"
        assert ack_deps.engine.transitions == []           # 没有状态迁移
        assert ack_deps.tracker.is_acked("cmd_tsk_frozen_001")

    def test_qos1_duplicate_does_not_advance_twice(self, ack_deps: SimpleNamespace) -> None:
        """★ 矩阵第 6 项：QoS1 重复 ACK 不重复推进。"""
        payload = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
        }
        assert run_ack(ack_deps, payload) == AckResult.NEW
        assert run_ack(ack_deps, payload) == AckResult.DUPLICATE
        assert ack_deps.task.status == "navigating"
        assert len(ack_deps.engine.transitions) == 1

    def test_unregistered_deadline_received_no_fake_late(self, ack_deps: SimpleNamespace) -> None:
        """★ 矩阵第 7 项：未登记 deadline 的命令仍可接收并推进，不伪判 late。"""
        payload = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 1e12,      # 远超任何真实时间
            "accepted": True,
        }
        assert ack_deps.tracker.has_deadline("cmd_tsk_frozen_001") is False
        outcome = run_ack(ack_deps, payload)
        assert outcome != AckResult.LATE
        assert outcome == AckResult.NEW
        assert ack_deps.task.status == "navigating"

    def test_late_ack_still_advances(self, ack_deps: SimpleNamespace) -> None:
        """late：received_at > expires_at 仍记录并允许推进（仅 assigned）。"""
        ack_deps.tracker.register_command("cmd_tsk_frozen_001", "RBT-001", 1, expires_at=100.0)
        payload = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 200.0,
            "accepted": True,
        }
        assert run_ack(ack_deps, payload) == AckResult.LATE
        assert ack_deps.task.status == "navigating"
        assert ack_deps.tracker.is_acked("cmd_tsk_frozen_001")

    def test_payload_device_mismatch_topic_rejected(self, ack_deps: SimpleNamespace) -> None:
        """★ 矩阵第 4 项：payload.device_id 与 topic robot_id 不一致 → 整条拒绝。"""
        payload = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-999",          # 与 topic RBT-001 不符
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
        }
        assert run_ack(ack_deps, payload) is None
        assert ack_deps.task.status == "assigned"
        assert ack_deps.engine.transitions == []
        assert ack_deps.tracker.is_acked("cmd_tsk_frozen_001") is False

    def test_topic_mismatch_task_robot_rejected(self, ack_deps: SimpleNamespace) -> None:
        """★ 矩阵第 4 项：topic robot_id 与 task.robot_id 不一致 → 整条拒绝。"""
        ack_deps.task.robot_id = "RBT-999"   # 任务其实派给了另一台
        payload = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
        }
        assert run_ack(ack_deps, payload) is None
        assert ack_deps.task.status == "assigned"
        assert ack_deps.engine.transitions == []
        assert ack_deps.tracker.is_acked("cmd_tsk_frozen_001") is False

    def test_unknown_task_rejected(self, ack_deps: SimpleNamespace) -> None:
        """未知 task：回执丢弃、不登记、不推进。"""
        ack_deps.task.task_id = "tsk_some_other"   # 报文指向不存在的任务
        payload = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
        }
        assert run_ack(ack_deps, payload) is None
        assert ack_deps.task.status == "assigned"
        assert ack_deps.engine.transitions == []
        assert ack_deps.tracker.is_acked("cmd_tsk_frozen_001") is False

    def test_unknown_command_rejected(self, ack_deps: SimpleNamespace) -> None:
        """未知 command 形态（无法反推 task_id）→ 丢弃。"""
        payload = {
            "command_id": "CMD-123",         # 非 cmd_{task_id} 且无顶层 task_id
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
        }
        assert run_ack(ack_deps, payload) is None
        assert ack_deps.task.status == "assigned"
        assert ack_deps.tracker.is_acked("CMD-123") is False

    def test_rejected_ack_does_not_poison_tracker(self, ack_deps: SimpleNamespace) -> None:
        """身份不一致的回执不登记 —— 后续合法回执仍按 new 处理并推进。"""
        bad = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-999",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
        }
        assert run_ack(ack_deps, bad) is None
        good = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
        }
        assert run_ack(ack_deps, good) == AckResult.NEW
        assert ack_deps.task.status == "navigating"

    def test_exception_isolated_to_warning(self, ack_deps: SimpleNamespace) -> None:
        """★ 任何异常隔离为 warning，不打断 MQTT 主循环（返回 None 不抛）。"""
        from app.mqtt.handlers import handle_robot_ack

        def boom():
            raise RuntimeError("db down")

        payload = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
        }
        result = asyncio.run(
            handle_robot_ack(
                ack_deps.topic,
                payload,
                tracker=ack_deps.tracker,
                session_factory=boom,
            )
        )
        assert result is None

    def test_bad_topic_returns_none(self, ack_deps: SimpleNamespace) -> None:
        from app.mqtt.handlers import handle_robot_ack

        payload = {
            "command_id": "cmd_tsk_frozen_001",
            "device_id": "RBT-001",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
        }
        result = asyncio.run(
            handle_robot_ack(
                "marine/lianjiang/CAM-01/event",
                payload,
                tracker=ack_deps.tracker,
                session_factory=ack_deps.session_factory,
            )
        )
        assert result is None
        assert ack_deps.task.status == "assigned"

    def test_malformed_payload_returns_none(self, ack_deps: SimpleNamespace) -> None:
        assert run_ack(ack_deps, "not-a-dict") is None
        assert run_ack(ack_deps, {"command_id": "cmd_x"}) is None      # 缺字段
        assert ack_deps.task.status == "assigned"


# ======================================================================
# client 接线：publish_task 发布成功后登记 AckTracker（WP-14C 条款 8）
# ======================================================================
class TestPublishTaskRegistersAckTracker:
    """★ 条款 8：仅在发布成功后登记 command_id/device_id/seq/expires_at。"""

    def _client(self, monkeypatch: pytest.MonkeyPatch, result: bool):
        from app.mqtt.client import MqttClient
        from app.mqtt.topics import Topics

        client = MqttClient()
        captured: dict = {}

        async def fake_publish(topic: str, payload: dict, *, qos: int = 1, retain: bool = False) -> bool:
            captured["topic"] = topic
            captured["payload"] = payload
            return result

        monkeypatch.setattr(client, "publish", fake_publish)
        return client, captured

    def test_publish_success_registers_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, captured = self._client(monkeypatch, result=True)
        ok = asyncio.run(
            client.publish_task("RBT-001", "tsk_reg_001", 119.6, 26.3, 1, ttl=30.0)
        )
        assert ok is True
        payload = captured["payload"]
        assert client.ack_tracker.has_deadline("cmd_tsk_reg_001") is True
        assert client.ack_tracker.deadline_for("cmd_tsk_reg_001") == payload["expires_at"]
        # 登记后可正常判 late
        env = AckEnvelope(
            command_id="cmd_tsk_reg_001",
            task_id="tsk_reg_001",
            device_id="RBT-001",
            seq=payload["seq"],
            received_at=payload["expires_at"] + 1.0,
            accepted=True,
            reason="",
            mode="",
        )
        assert client.ack_tracker.classify(env) == AckResult.LATE

    def test_publish_failure_does_not_register(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = self._client(monkeypatch, result=False)
        ok = asyncio.run(
            client.publish_task("RBT-001", "tsk_reg_002", 119.6, 26.3, 1)
        )
        assert ok is False
        assert client.ack_tracker.has_deadline("cmd_tsk_reg_002") is False

    def test_publish_registers_device_and_seq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, captured = self._client(monkeypatch, result=True)
        asyncio.run(client.publish_task("RBT-001", "tsk_reg_003", 119.6, 26.3, 1))
        payload = captured["payload"]
        env = AckEnvelope(
            command_id=payload["command_id"],
            task_id="tsk_reg_003",
            device_id="RBT-001",
            seq=payload["seq"],
            received_at=1.0,
            accepted=True,
            reason="",
            mode="",
        )
        # 未超过 expires_at → new（登记只影响 late，不影响接收）
        assert client.ack_tracker.record(env) == AckResult.NEW
