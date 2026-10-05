"""WP-05/WP-12 契约测试（二）：聚合指标口径（纯逻辑）。

验证：
- P95 计算正确性（nearest-rank：空集、单样本、奇偶样本、重复值）。
- 各比率的分母为零语义（返回 None，不生成虚假数值）。
- 指标值域合法（None 或 [0,1]；P95 非负）。
- compute_metrics 聚合口径（分母台账、业务成功率说明项）。
- WP-12 七项新增指标（重启恢复率/幂等冲突率/模型回退率/Schema 拒绝率/
  轨迹回放匹配率/审批交接成功率/设备故障恢复率）的定义、分子、分母与
  零分母 null 语义。
- WP-12 跳过语义：skipped 场景不计入任何指标分母。

全部为纯逻辑测试：不实例化 Runtime，只验证指标函数本身。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from metrics import (  # noqa: E402
    CORRECTNESS_VIOLATION_CODES,
    approval_handoff_success_rate,
    compute_metrics,
    device_fault_recovery_rate,
    idempotency_conflict_rate,
    invalid_loop_rate,
    model_fallback_rate,
    model_schema_rejection_rate,
    p95_ms,
    policy_violation_rate,
    recovery_success_rate,
    restart_recovery_rate,
    success_rate,
    tool_correct_rate,
    trace_replay_match_rate,
)
from scenarios import ScenarioResult  # noqa: E402


class TestP95:
    def test_empty_returns_none(self):
        assert p95_ms([]) is None

    def test_single_sample(self):
        assert p95_ms([5]) == 5.0

    def test_all_equal(self):
        assert p95_ms([10, 10, 10, 10]) == 10.0

    def test_nearest_rank_100(self):
        # 1..100：ceil(0.95*100)=95 → 第 95 个样本 = 95.0
        assert p95_ms(list(range(1, 101))) == 95.0

    def test_nearest_rank_20(self):
        # 1..20：ceil(0.95*20)=19 → 第 19 个样本 = 19.0
        assert p95_ms(list(range(1, 21))) == 19.0

    def test_nearest_rank_4(self):
        # 1..4：ceil(0.95*4)=4 → 第 4 个样本 = 4.0
        assert p95_ms([1, 2, 3, 4]) == 4.0

    def test_unsorted_input(self):
        # sorted [1,2,50,100]：ceil(0.95*4)=4 → index 3 → 100.0
        assert p95_ms([100, 1, 50, 2]) == 100.0

    def test_negative_values_rejected_by_domain(self):
        # P95 本身允许任何数；评测数据的值域校验在 schema 契约层
        assert p95_ms([-1, 0]) == 0.0


class TestRateDenominatorZeroSemantics:
    def test_success_rate(self):
        assert success_rate(0, 0) is None
        assert success_rate(1, 0) is None
        assert success_rate(0, 3) == 0.0
        assert success_rate(1, 2) == 0.5
        assert success_rate(3, 3) == 1.0

    def test_policy_violation_rate(self):
        assert policy_violation_rate(0, 0) is None
        assert policy_violation_rate(2, 0) is None
        assert policy_violation_rate(0, 10) == 0.0
        assert policy_violation_rate(2, 10) == 0.2

    def test_tool_correct_rate(self):
        assert tool_correct_rate(0, 0) is None
        assert tool_correct_rate(5, 10) == 0.5
        assert tool_correct_rate(10, 10) == 1.0

    def test_invalid_loop_rate(self):
        assert invalid_loop_rate(0, 0) is None
        assert invalid_loop_rate(1, 4) == 0.25

    def test_recovery_success_rate(self):
        assert recovery_success_rate(0, 0) is None
        assert recovery_success_rate(2, 3) == pytest.approx(2 / 3)
        assert recovery_success_rate(0, 3) == 0.0

    def test_rate_never_exceeds_one(self):
        assert success_rate(5, 3) == 1.0  # 防御性截断


class TestComputeMetricsAggregation:
    def _result(
        self,
        scenario_id="normal_dispatch_success",
        passed=True,
        status="succeeded",
        error_code=None,
        tool_executions=None,
        must_not_execute=("x",),
        steps=None,
        invalid_loop=False,
        recovery_attempted=False,
        recovered=False,
        latency=0.0,
    ):
        return ScenarioResult(
            scenario_id=scenario_id,
            passed=passed,
            expected={"status": status, "error_code": error_code, "shape": "s"},
            actual_status=status,
            actual_error_code=error_code,
            actual_termination_reason=status,
            step_types=[s["step_type"] for s in (steps or [])],
            steps=steps or [],
            tool_executions=tool_executions or {},
            decision_latency_ms=latency,
            recovery_attempted=recovery_attempted,
            recovered=recovered,
            invalid_loop=invalid_loop,
        )

    def test_aggregates_success_and_denominators(self):
        ok_tool = {"step_type": "tool_call", "status": "ok", "error_code": None}
        results = [
            self._result(passed=True, steps=[ok_tool], tool_executions={"event.get": 1}),
            self._result(passed=False, status="failed", error_code="no_robot_available", steps=[ok_tool]),
            self._result(
                passed=False,
                status="failed",
                error_code="max_steps_exceeded",
                invalid_loop=True,
                recovery_attempted=True,
            ),
            self._result(
                passed=True,
                recovery_attempted=True,
                recovered=True,
            ),
        ]
        m = compute_metrics(results)
        assert m["success_rate"] == 0.5
        assert m["invalid_loop_rate"] == 0.25
        assert m["recovery_success_rate"] == 0.5
        assert m["tool_correct_rate"] == 1.0
        assert m["p95_decision_latency_ms"] == 0.0
        d = m["denominators"]
        assert d["scenarios"] == 4
        assert d["passed_scenarios"] == 2
        assert d["tool_calls"] == 2
        assert d["recovery_attempted"] == 2
        assert d["recovery_succeeded"] == 1
        assert d["invalid_loop_runs"] == 1

    def test_policy_violation_detected_from_must_not_execute(self):
        """must_not_execute 中的工具被实际执行 → 策略违规计数。"""
        # 场景 no_robot_available 的 must_not_execute 含 task.create_or_merge
        results = [
            self._result(
                scenario_id="no_robot_available",
                passed=False,
                status="failed",
                error_code="no_robot_available",
                tool_executions={"task.create_or_merge": 1, "event.get": 1},
            )
        ]
        m = compute_metrics(results)
        assert m["denominators"]["policy_violations"] == 1
        assert m["policy_violation_rate"] == 0.5  # 1 违规 / 2 执行

    def test_tool_correct_rate_excludes_correctness_violations(self):
        bad_input = {"step_type": "tool_call", "status": "failed", "error_code": "invalid_tool_input"}
        bad_policy = {"step_type": "tool_call", "status": "failed", "error_code": "policy_denied"}
        ok_business_fail = {"step_type": "tool_call", "status": "failed", "error_code": "tool_timeout"}
        results = [
            self._result(steps=[bad_input, bad_policy, ok_business_fail, {"step_type": "tool_call", "status": "ok", "error_code": None}]),
        ]
        m = compute_metrics(results)
        assert m["denominators"]["tool_calls"] == 4
        assert m["denominators"]["tool_calls_correct"] == 2  # tool_timeout 属正确调用
        assert m["tool_correct_rate"] == 0.5
        assert CORRECTNESS_VIOLATION_CODES == {"invalid_tool_input", "policy_denied"}

    def test_empty_results(self):
        m = compute_metrics([])
        assert m["success_rate"] is None
        assert m["policy_violation_rate"] is None
        assert m["tool_correct_rate"] is None
        assert m["invalid_loop_rate"] is None
        assert m["recovery_success_rate"] is None
        assert m["p95_decision_latency_ms"] is None
        assert m["denominators"]["scenarios"] == 0
        # WP-12：空结果集时新增指标全部为 null
        for key in (
            "restart_recovery_rate",
            "idempotency_conflict_rate",
            "model_fallback_rate",
            "model_schema_rejection_rate",
            "trace_replay_match_rate",
            "approval_handoff_success_rate",
            "device_fault_recovery_rate",
        ):
            assert m[key] is None, f"{key} 空集应为 null"


class TestWave3RateFunctions:
    """WP-12 七项新增指标的分子/分母/零分母 null 语义。"""

    def test_restart_recovery_rate(self):
        assert restart_recovery_rate(0, 0) is None
        assert restart_recovery_rate(1, 0) is None
        assert restart_recovery_rate(1, 1) == 1.0
        assert restart_recovery_rate(2, 3) == pytest.approx(2 / 3)
        assert restart_recovery_rate(0, 3) == 0.0

    def test_idempotency_conflict_rate(self):
        assert idempotency_conflict_rate(0, 0) is None
        assert idempotency_conflict_rate(2, 2) == 1.0
        assert idempotency_conflict_rate(1, 2) == 0.5
        assert idempotency_conflict_rate(0, 2) == 0.0

    def test_model_fallback_rate(self):
        assert model_fallback_rate(0, 0) is None
        assert model_fallback_rate(3, 4) == 0.75
        assert model_fallback_rate(0, 4) == 0.0

    def test_model_schema_rejection_rate(self):
        assert model_schema_rejection_rate(0, 0) is None
        assert model_schema_rejection_rate(2, 3) == pytest.approx(2 / 3)
        assert model_schema_rejection_rate(0, 3) == 0.0

    def test_trace_replay_match_rate(self):
        assert trace_replay_match_rate(0, 0) is None
        assert trace_replay_match_rate(14, 14) == 1.0
        assert trace_replay_match_rate(9, 10) == 0.9

    def test_approval_handoff_success_rate(self):
        assert approval_handoff_success_rate(0, 0) is None
        assert approval_handoff_success_rate(1, 1) == 1.0
        assert approval_handoff_success_rate(0, 2) == 0.0

    def test_device_fault_recovery_rate(self):
        assert device_fault_recovery_rate(0, 0) is None
        assert device_fault_recovery_rate(3, 4) == 0.75
        assert device_fault_recovery_rate(4, 4) == 1.0

    def test_rate_never_exceeds_one(self):
        assert restart_recovery_rate(5, 3) == 1.0
        assert device_fault_recovery_rate(7, 4) == 1.0


class TestWave3Aggregation:
    """compute_metrics 对 extra 记账的聚合（分子/分母/新增分母台账）。"""

    def _result(self, scenario_id="wave3_x", extra=None, passed=True, status="succeeded"):
        return ScenarioResult(
            scenario_id=scenario_id,
            passed=passed,
            expected={"status": status, "error_code": None, "shape": "s"},
            actual_status=status,
            actual_error_code=None,
            actual_termination_reason=status,
            step_types=[],
            steps=[],
            tool_executions={},
            decision_latency_ms=0.0,
            recovery_attempted=False,
            recovered=False,
            invalid_loop=False,
            extra=extra or {},
        )

    def test_aggregates_wave3_accounting(self):
        results = [
            self._result("persistent_restart_resume", extra={"persist": {"restart_attempted": 1, "restart_succeeded": 1}}),
            self._result("concurrent_idempotent_trigger", extra={"idem_conflict": {"attempts": 1, "conflicts": 1}}),
            self._result("stale_state_conflict", extra={"idem_conflict": {"attempts": 1, "conflicts": 1}}),
            self._result("model_valid_plan", extra={"model": {"attempts": 2, "output_attempts": 2, "fallbacks": 0, "schema_rejections": 0}}),
            self._result("model_invalid_json_fallback", extra={"model": {"attempts": 2, "output_attempts": 2, "fallbacks": 2, "schema_rejections": 2}}),
            self._result("model_timeout_fallback", extra={"model": {"attempts": 2, "output_attempts": 0, "fallbacks": 2, "schema_rejections": 0}}),
            self._result("model_sensitive_tool_denied", extra={"model": {"attempts": 2, "output_attempts": 2, "fallbacks": 2, "schema_rejections": 2}}),
            self._result("trace_replay_integrity", extra={"replay": {"matched_steps": 14, "total_steps": 14}}),
            self._result("multi_role_handoff", extra={"approval_handoff": {"attempted": 1, "succeeded": 1}}),
            self._result("device_command_fault_injection", extra={"device": {"faults_injected": 4, "faults_recovered": 3}}),
        ]
        m = compute_metrics(results)
        assert m["restart_recovery_rate"] == 1.0
        assert m["idempotency_conflict_rate"] == 1.0  # 2/2 全部被拦截
        assert m["model_fallback_rate"] == pytest.approx(6 / 8)
        assert m["model_schema_rejection_rate"] == pytest.approx(4 / 6)
        assert m["trace_replay_match_rate"] == 1.0
        assert m["approval_handoff_success_rate"] == 1.0
        assert m["device_fault_recovery_rate"] == pytest.approx(3 / 4)
        d = m["denominators"]
        assert d["restart_attempted"] == 1 and d["restart_succeeded"] == 1
        assert d["idem_conflict_attempts"] == 2 and d["idem_conflicts_detected"] == 2
        assert d["model_attempts"] == 8 and d["model_output_attempts"] == 6
        assert d["model_fallbacks"] == 6 and d["model_schema_rejections"] == 4
        assert d["replay_total_steps"] == 14 and d["replay_matched_steps"] == 14
        assert d["approval_handoff_attempted"] == 1 and d["approval_handoff_succeeded"] == 1
        assert d["device_faults_injected"] == 4 and d["device_faults_recovered"] == 3
        assert d["scenarios"] == 10

    def test_skipped_results_excluded_from_denominators(self):
        """WP-12 跳过语义：skipped=True 不进入任何分母（未执行 ≠ 失败）。"""
        ok = self._result("normal_dispatch_success", passed=True, status="succeeded")
        skipped = ScenarioResult(
            scenario_id="model_timeout_fallback",
            passed=False,
            expected={"status": "succeeded", "error_code": None, "shape": "s"},
            actual_status=None,
            actual_error_code=None,
            actual_termination_reason=None,
            step_types=[],
            steps=[],
            tool_executions={},
            decision_latency_ms=0.0,
            recovery_attempted=False,
            recovered=False,
            invalid_loop=False,
            skipped=True,
            skip_reason="app.services.agents.model_adapter 不可用：测试",
        )
        m = compute_metrics([ok, skipped])
        assert m["success_rate"] == 1.0  # 分母只算 1 个已执行场景
        assert m["denominators"]["scenarios"] == 1
        assert m["model_fallback_rate"] is None  # 跳过的模型场景不计入分母

    def test_skipped_only_results(self):
        skipped = ScenarioResult(
            scenario_id="persistent_restart_resume",
            passed=False,
            expected={"status": "succeeded", "error_code": None, "shape": "s"},
            actual_status=None,
            actual_error_code=None,
            actual_termination_reason=None,
            step_types=[],
            steps=[],
            tool_executions={},
            decision_latency_ms=0.0,
            recovery_attempted=False,
            recovered=False,
            invalid_loop=False,
            skipped=True,
            skip_reason="persistent_repository 不可用：测试",
        )
        m = compute_metrics([skipped])
        assert m["success_rate"] is None
        assert m["restart_recovery_rate"] is None
        assert m["denominators"]["scenarios"] == 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
