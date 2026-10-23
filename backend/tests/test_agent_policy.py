"""策略守卫 / 记忆 / 审批与运行仓储 / ID 工厂 测试（WP-01）。

覆盖：
- RulePolicyGuard：角色守卫、审批阈值、计划聚合预检、策略版本
- PolicyDecision 判定形状
- InMemoryMemoryStore：增查、解密预算禁止键守卫、mem_ 前缀、唯一约束
- InMemoryApprovalRepository：创建/查询/决策/唯一约束/apr_ 前缀
- InMemoryRunRepository：唯一约束（run_id、step_id、(run_id, step_no)）、
  幂等键索引
- SequenceIdFactory / FakeClock：确定性可复现
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.agents import (  # noqa: E402
    AgentError,
    AgentNotFoundError,
    AgentRun,
    AgentRunRequest,
    AgentStep,
    ApprovalRecord,
    FakeClock,
    InMemoryApprovalRepository,
    InMemoryMemoryStore,
    InMemoryRunRepository,
    MemoryEntry,
    PolicyDecision,
    RiskLevel,
    RulePolicyGuard,
    RuntimeConfig,
    SequenceIdFactory,
    TaskConflictError,
    ToolDefinition,
    ToolRegistry,
)


def make_tool(name="demo.tool", *, risk=RiskLevel.READ_ONLY, roles=("operator",)):
    return ToolDefinition(
        name=name,
        version="1.0.0",
        description="策略测试工具",
        input_schema={"type": "object", "required": [], "properties": {}},
        output_schema={"type": "object", "required": [], "properties": {}},
        risk_level=risk,
        timeout_ms=5000,
        idempotent=False,
        allowed_roles=roles,
        handler=lambda ctx: {},
    )


# ======================================================================
# RulePolicyGuard
# ======================================================================


class TestRulePolicyGuard:
    def test_allowed_role_allow(self):
        guard = RulePolicyGuard(RuntimeConfig())
        decision = guard.check_tool(make_tool(), "operator", {})
        assert decision.allow is True
        assert decision.requires_approval is False
        assert decision.policy_version == "rules-v1.0"

    def test_disallowed_role_deny(self):
        guard = RulePolicyGuard(RuntimeConfig())
        decision = guard.check_tool(make_tool(roles=("dispatcher",)), "operator", {})
        assert decision.allow is False
        assert "无权" in decision.reason

    def test_blocked_tool_deny(self):
        guard = RulePolicyGuard(RuntimeConfig(), blocked_tools=("danger.tool",))
        decision = guard.check_tool(make_tool(name="danger.tool"), "operator", {})
        assert decision.allow is False
        assert "黑名单" in decision.reason

    def test_sensitive_risk_requires_approval(self):
        guard = RulePolicyGuard(RuntimeConfig())
        decision = guard.check_tool(
            make_tool(risk=RiskLevel.SENSITIVE), "operator", {}
        )
        assert decision.allow is True
        assert decision.requires_approval is True
        assert decision.risk_level == RiskLevel.SENSITIVE

    def test_read_only_and_write_do_not_require_approval(self):
        guard = RulePolicyGuard(RuntimeConfig())
        for risk in (RiskLevel.READ_ONLY, RiskLevel.WRITE):
            decision = guard.check_tool(make_tool(risk=risk), "operator", {})
            assert decision.requires_approval is False

    def test_device_command_requires_approval(self):
        guard = RulePolicyGuard(RuntimeConfig())
        decision = guard.check_tool(
            make_tool(risk=RiskLevel.DEVICE_COMMAND), "operator", {}
        )
        assert decision.requires_approval is True

    def test_check_plan_aggregates_role_denials(self):
        registry = ToolRegistry()
        registry.register(make_tool(name="a.tool"))
        registry.register(make_tool(name="b.tool", roles=("dispatcher",)))
        guard = RulePolicyGuard(RuntimeConfig())
        request = AgentRunRequest(trigger_type="event", objective="o", role="operator")
        from app.services.agents import RuleStep

        plan = [
            RuleStep(tool="a.tool", input={}, summary="s"),
            RuleStep(tool="b.tool", input={}, summary="s"),
        ]
        decision = guard.check_plan(plan, request, registry)
        assert decision.allow is False
        assert "b.tool" in decision.reason

    def test_check_plan_allows_clean_plan(self):
        registry = ToolRegistry()
        registry.register(make_tool(name="a.tool"))
        guard = RulePolicyGuard(RuntimeConfig())
        from app.services.agents import RuleStep

        plan = [RuleStep(tool="a.tool", input={}, summary="s")]
        decision = guard.check_plan(
            plan, AgentRunRequest(trigger_type="event", objective="o"), registry
        )
        assert decision.allow is True
        assert decision.policy_version == "rules-v1.0"

    def test_policy_decision_shapes(self):
        d1 = PolicyDecision.allow_decision()
        assert d1.allow and d1.reason
        d2 = PolicyDecision.deny_decision("no")
        assert d2.allow is False and d2.reason == "no"


# ======================================================================
# InMemoryMemoryStore（解密预算守卫）
# ======================================================================


class TestMemoryStore:
    def make_store(self):
        return InMemoryMemoryStore(id_factory=SequenceIdFactory(), clock=FakeClock())

    def test_save_get_find(self):
        store = self.make_store()
        entry = MemoryEntry(
            memory_id="",
            memory_type="working",
            scope_type="run",
            scope_id="run_1",
            content={"summary": "事件 evt_001 已确认", "business_reason": "置信度 0.9"},
            confidence=1.0,
            source_type="tool_call",
            source_id="stp_1",
            valid_from=FakeClock().now(),
        )
        saved = store.save(entry)
        assert saved.memory_id.startswith("mem_")
        assert store.get(saved.memory_id) is saved
        found = store.find(scope_type="run", scope_id="run_1")
        assert len(found) == 1
        assert store.find(scope_type="run", scope_id="run_2") == []

    def test_forbidden_chain_of_thought_rejected(self):
        store = self.make_store()
        entry = MemoryEntry(
            memory_id="",
            memory_type="working",
            scope_type="run",
            scope_id="run_1",
            content={"chain_of_thought": "模型的私有思考……", "summary": "摘要"},
        )
        with pytest.raises(AgentError) as exc:
            store.save(entry)
        assert "解密预算" in str(exc.value) or "禁止字段" in str(exc.value)

    def test_invalid_memory_type_rejected(self):
        store = self.make_store()
        with pytest.raises(AgentError):
            store.save(
                MemoryEntry(
                    memory_id="",
                    memory_type="hallucination",
                    scope_type="run",
                    scope_id="run_1",
                    content={"summary": "x"},
                )
            )

    def test_duplicate_memory_id_conflict(self):
        store = self.make_store()
        entry = MemoryEntry(
            memory_id="mem_1",
            memory_type="working",
            scope_type="run",
            scope_id="run_1",
            content={"summary": "a"},
        )
        store.save(entry)
        with pytest.raises(TaskConflictError):
            store.save(
                MemoryEntry(
                    memory_id="mem_1",
                    memory_type="working",
                    scope_type="run",
                    scope_id="run_2",
                    content={"summary": "b"},
                )
            )


# ======================================================================
# InMemoryApprovalRepository
# ======================================================================


class TestApprovalRepository:
    def make_record(self, approval_id="apr_1"):
        return ApprovalRecord(
            approval_id=approval_id,
            run_id="run_1",
            requested_action="mqtt.send_task：下发设备指令",
            risk_level=RiskLevel.SENSITIVE.value,
            requested_by="operator-01",
            requested_at=datetime(2026, 1, 1),
        )

    def test_create_list_pending_decide(self):
        repo = InMemoryApprovalRepository()
        record = repo.create(self.make_record())
        assert record.approval_id.startswith("apr_")
        assert len(repo.list_pending()) == 1
        decided = repo.decide("apr_1", "admin", "approved", "确认")
        assert decided.decision == "approved"
        assert decided.decided_by == "admin"
        assert len(repo.list_pending()) == 0

    def test_duplicate_approval_conflict(self):
        repo = InMemoryApprovalRepository()
        repo.create(self.make_record())
        with pytest.raises(TaskConflictError):
            repo.create(self.make_record())

    def test_decide_unknown_raises(self):
        repo = InMemoryApprovalRepository()
        with pytest.raises(AgentNotFoundError):
            repo.decide("apr_missing", "admin", "approved")

    def test_decide_twice_conflict(self):
        repo = InMemoryApprovalRepository()
        repo.create(self.make_record())
        repo.decide("apr_1", "admin", "approved")
        with pytest.raises(TaskConflictError):
            repo.decide("apr_1", "admin", "rejected")

    def test_count(self):
        repo = InMemoryApprovalRepository()
        repo.create(self.make_record("apr_1"))
        repo.create(self.make_record("apr_2"))
        assert repo.count() == 2


# ======================================================================
# InMemoryRunRepository
# ======================================================================


class TestRunRepository:
    def make_run(self, run_id="run_1"):
        now = datetime(2026, 1, 1)
        return AgentRun(
            run_id=run_id,
            trigger_type="event",
            objective="o",
            status="created",
            request=AgentRunRequest(
                trigger_type="event", objective="o", params={}, idempotency_key=None
            ),
            started_at=now,
            created_at=now,
            updated_at=now,
        )

    def test_create_get_list(self):
        repo = InMemoryRunRepository()
        repo.create(self.make_run())
        assert repo.get("run_1") is not None
        assert repo.get("missing") is None
        assert repo.count() == 1
        assert len(repo.list()) == 1

    def test_run_id_unique(self):
        repo = InMemoryRunRepository()
        repo.create(self.make_run())
        with pytest.raises(TaskConflictError):
            repo.create(self.make_run())

    def test_step_uniqueness(self):
        repo = InMemoryRunRepository()
        repo.create(self.make_run())
        repo.add_step(
            AgentStep(
                step_id="stp_1", run_id="run_1", step_no=1, step_type="plan",
                decision_summary="s", created_at=datetime(2026, 1, 1),
            )
        )
        with pytest.raises(TaskConflictError):
            repo.add_step(
                AgentStep(
                    step_id="stp_2", run_id="run_1", step_no=1, step_type="plan",
                    decision_summary="s", created_at=datetime(2026, 1, 1),
                )
            )  # (run_id, step_no) 重复
        with pytest.raises(TaskConflictError):
            repo.add_step(
                AgentStep(
                    step_id="stp_1", run_id="run_1", step_no=2, step_type="plan",
                    decision_summary="s", created_at=datetime(2026, 1, 1),
                )
            )  # step_id 重复
        assert len(repo.steps("run_1")) == 1

    def test_idempotency_key_index(self):
        repo = InMemoryRunRepository()
        run = self.make_run()
        run.request.idempotency_key = "evt_001"
        repo.create(run)
        assert repo.find_by_idempotency_key("evt_001").run_id == "run_1"
        assert repo.find_by_idempotency_key("evt_002") is None
        with pytest.raises(TaskConflictError):
            other = self.make_run("run_2")
            other.request.idempotency_key = "evt_001"
            repo.create(other)

    def test_steps_sorted_by_step_no(self):
        repo = InMemoryRunRepository()
        repo.create(self.make_run())
        repo.add_step(AgentStep(step_id="stp_a", run_id="run_1", step_no=2, step_type="plan", decision_summary="b"))
        repo.add_step(AgentStep(step_id="stp_b", run_id="run_1", step_no=1, step_type="plan", decision_summary="a"))
        assert [s.step_no for s in repo.steps("run_1")] == [1, 2]


# ======================================================================
# 确定性工厂
# ======================================================================


class TestDeterministicFactories:
    def test_sequence_id_factory_reproducible(self):
        f1 = SequenceIdFactory()
        f2 = SequenceIdFactory()
        assert [f1.new("run_") for _ in range(3)] == ["run_0001", "run_0002", "run_0003"]
        assert [f2.new("stp_") for _ in range(3)] == ["stp_0001", "stp_0002", "stp_0003"]

    def test_fake_clock_now_and_now_ms_consistent(self):
        clock = FakeClock(start=datetime(2026, 6, 1, 12, 0, 0))
        t0 = clock.now()
        m0 = clock.now_ms()
        clock.advance(2500)
        assert (clock.now() - t0).total_seconds() == 2.5
        assert clock.now_ms() - m0 == 2500
