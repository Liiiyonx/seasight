"""WP-05/WP-12 聚合指标：纯逻辑函数（口径可单测、分母为零有明确定义）。

WP-05 六项聚合指标（口径冻结，见计划书 5.13）：
    - success_rate            期望结果达成的场景数 / 场景总数
    - policy_violation_rate   被守卫拦截但工具仍执行的次数 / 工具实际执行次数
    - tool_correct_rate       参数/权限/幂等均正确的工具调用数 / 工具调用步骤总数
    - invalid_loop_rate       超步数或重复无进展步骤的 run 数 / run 总数
    - recovery_success_rate   可恢复异常中成功恢复的 run 数 / 尝试恢复的 run 数
    - p95_decision_latency_ms 从触发到首个可执行计划产出的耗时（毫秒）P95

WP-12 第三波新增七项聚合指标（docs/agent-program-wave3.md 3.3）：
    - restart_recovery_rate         重启后成功续跑的 run 数 / 需要重启续跑的 run 数
    - idempotency_conflict_rate     幂等键/乐观锁冲突被正确拒绝的尝试数 / 冲突写入尝试总数
    - model_fallback_rate           模型规划回退规则模式的次数 / 模型规划尝试总次数
    - model_schema_rejection_rate   模型输出被严格校验链拒绝的回退次数 / 模型实际返回输出的尝试次数
    - trace_replay_match_rate       回放与原始轨迹匹配的步骤数 / 回放总步骤数
    - approval_handoff_success_rate 跨角色审批交接成功完成的次数 / 需要跨角色交接的审批次数
    - device_fault_recovery_rate    成功恢复的注入故障数 / 注入故障总数（无故障时 null）

分母为零语义：所有比率在分母为 0 时返回 None（JSON 输出 null），
不生成虚假数值（计划书 7.1：没有可靠分母禁止估数）。

★ WP-12 跳过语义：`ScenarioResult.skipped=True` 的场景明确跳过（依赖接口
缺失等原因），不计入任何指标的分母（避免把「未执行」算成「失败」）。

P95 采用 nearest-rank 法：升序排列后取 ceil(0.95*n) 位置的样本值。
"""

from __future__ import annotations

import math
from typing import Any

from scenarios import ScenarioResult

# 工具调用「正确性违规」错误码：参数 schema / 权限 / 幂等链上的违规。
# 业务结果失败（tool_timeout / tool_failed / no_robot_available /
# invalid_tool_output 等）不属于「调用正确性」违规 —— 调用本身经过了
# 参数校验、权限守卫与幂等检查，失败是执行或业务期望问题（计入成功率与
# 恢复成功率口径）。
CORRECTNESS_VIOLATION_CODES = frozenset({"invalid_tool_input", "policy_denied"})

# 模型输出被严格校验链拒绝的回退错误码（与 scenarios.MODEL_SCHEMA_REJECTION_CODES 同源）
MODEL_SCHEMA_REJECTION_CODES = frozenset(
    {
        "model_invalid_json",
        "model_schema_error",
        "model_unknown_tool",
        "model_disallowed_argument",
        "model_role_denied",
        "model_forbidden_field",
    }
)

METRIC_DEFINITIONS: dict[str, str] = {
    "success_rate": "期望结果达成的场景数 / 场景总数（通过数含预期失败的负向场景；跳过场景不计分母）",
    "policy_violation_rate": "被守卫拦截但工具仍执行的次数 / 工具实际执行次数（handler 真实调用计数）",
    "tool_correct_rate": "参数/权限/幂等均正确的工具调用数 / 工具调用步骤总数（业务结果失败不计入违规）",
    "invalid_loop_rate": "超步数或重复无进展步骤（终态 max_steps_exceeded）的 run 数 / run 总数",
    "recovery_success_rate": "可恢复异常中成功恢复（含 replan 且最终 succeeded）的 run 数 / 尝试恢复的 run 数",
    "p95_decision_latency_ms": "从触发（run 创建）到首个可执行计划产出（首条 plan 步骤）的耗时毫秒，nearest-rank P95",
    "restart_recovery_rate": "重启后成功续跑的 run 数 / 需要重启续跑的 run 数（重启 = 全新仓储实例 + 全新 runtime 从同一数据库 resume）",
    "idempotency_conflict_rate": "幂等键/乐观锁冲突被正确拒绝（抛 TaskConflictError）的尝试数 / 冲突写入尝试总数（越高越好，1.0 = 全部冲突被拦截）",
    "model_fallback_rate": "模型规划回退规则模式（source=rule_fallback）的次数 / 模型规划尝试总次数（每次 plan() 调用记 1，含禁用/超时/非法输出）",
    "model_schema_rejection_rate": "模型输出被严格校验链拒绝的回退次数 / 模型实际返回输出的尝试次数（不含超时/连接类失败）",
    "trace_replay_match_rate": "回放与原始轨迹匹配的步骤数 / 回放总步骤数（比较步骤类型/工具/错误码/终态/摘要哈希，不比较原始思维链）",
    "approval_handoff_success_rate": "跨角色审批交接成功完成的次数 / 需要跨角色交接的审批次数（requested_by 与 decided_by 角色不同）",
    "device_fault_recovery_rate": "成功恢复的注入故障数 / 注入故障总数（无故障时 null，取自 WP-14 确定性 FaultReport）",
}


def p95_ms(values: list[float]) -> float | None:
    """nearest-rank P95；空列表返回 None（分母为零语义）。"""
    if not values:
        return None
    sorted_values = sorted(values)
    index = max(int(math.ceil(0.95 * len(sorted_values))) - 1, 0)
    return float(sorted_values[index])


def success_rate(passed: int, total: int) -> float | None:
    return _rate(passed, total)


def policy_violation_rate(violations: int, executions: int) -> float | None:
    return _rate(violations, executions)


def tool_correct_rate(correct: int, total_calls: int) -> float | None:
    return _rate(correct, total_calls)


def invalid_loop_rate(loop_runs: int, total_runs: int) -> float | None:
    return _rate(loop_runs, total_runs)


def recovery_success_rate(recovered: int, attempted: int) -> float | None:
    return _rate(recovered, attempted)


# ---- WP-12 第三波新增指标（每个都有定义、分子、分母与零分母 null 语义） ----


def restart_recovery_rate(succeeded: int, attempted: int) -> float | None:
    """重启续跑成功率：成功续跑 run / 需要重启续跑 run；无重启尝试 → null。"""
    return _rate(succeeded, attempted)


def idempotency_conflict_rate(conflicts: int, attempts: int) -> float | None:
    """幂等/乐观锁冲突被正确拒绝率：拒绝次数 / 冲突尝试次数；无冲突尝试 → null。"""
    return _rate(conflicts, attempts)


def model_fallback_rate(fallbacks: int, attempts: int) -> float | None:
    """模型回退率：回退规则规划次数 / 模型规划尝试次数；无尝试 → null。"""
    return _rate(fallbacks, attempts)


def model_schema_rejection_rate(rejections: int, output_attempts: int) -> float | None:
    """模型输出被严格校验拒绝率：拒绝次数 / 模型实际返回输出的尝试次数；无输出 → null。"""
    return _rate(rejections, output_attempts)


def trace_replay_match_rate(matched: int, total: int) -> float | None:
    """轨迹回放匹配率：匹配步骤数 / 总步骤数；总步骤为零 → null。"""
    return _rate(matched, total)


def approval_handoff_success_rate(succeeded: int, attempted: int) -> float | None:
    """跨角色审批交接成功率：成功交接数 / 需要交接的审批数；无交接 → null。"""
    return _rate(succeeded, attempted)


def device_fault_recovery_rate(recovered: int, injected: int) -> float | None:
    """设备故障恢复率：恢复故障数 / 注入故障总数；无注入故障 → null。"""
    return _rate(recovered, injected)


def _rate(numerator: int, denominator: int) -> float | None:
    """比率：分母为 0 返回 None；分子越界视为 0（防御，正常不会发生）。"""
    if denominator <= 0:
        return None
    if numerator < 0:
        numerator = 0
    return min(float(numerator) / float(denominator), 1.0)


# ----------------------------------------------------------------------
# 聚合入口：把 ScenarioResult 列表压成指标字典（脚本与契约测试共用）
# ----------------------------------------------------------------------


def _sum_extra(results: list[ScenarioResult], key: str, field: str) -> int:
    """对每个结果的 extra[key][field] 求和（防御：缺省视为 0）。"""
    total = 0
    for r in results:
        bucket = r.extra.get(key) if isinstance(r.extra, dict) else None
        if isinstance(bucket, dict):
            total += int(bucket.get(field, 0) or 0)
    return total


def compute_metrics(results: list[ScenarioResult]) -> dict[str, Any]:
    """从场景结果计算全部指标 + 说明性业务成功率 + 分母台账。

    WP-12：`skipped=True` 的结果不参与任何分母（明确跳过，不算失败）。
    """
    executed = [r for r in results if not r.skipped]
    total = len(executed)
    passed = sum(1 for r in executed if r.passed)

    tool_calls: list[dict[str, Any]] = []
    tool_executions = 0
    for r in executed:
        for s in _steps_of(r):
            if s["step_type"] == "tool_call":
                tool_calls.append(s)
        tool_executions += sum(r.tool_executions.values())
    correct = sum(1 for s in tool_calls if s.get("error_code") not in CORRECTNESS_VIOLATION_CODES)

    violations = 0
    for r in executed:
        violations += sum(
            count for tool, count in r.tool_executions.items()
            if tool in _must_not_execute_of(r)
        )

    loop_runs = sum(1 for r in executed if r.invalid_loop)
    recovered = sum(1 for r in executed if r.recovered)
    attempted = sum(1 for r in executed if r.recovery_attempted)
    business_ok = sum(1 for r in executed if r.actual_status == "succeeded")

    latencies = [r.decision_latency_ms for r in executed]

    # ---- WP-12 第三波聚合（分子/分母台账） ----
    restart_succeeded = _sum_extra(executed, "persist", "restart_succeeded")
    restart_attempted = _sum_extra(executed, "persist", "restart_attempted")
    idem_conflicts = _sum_extra(executed, "idem_conflict", "conflicts")
    idem_attempts = _sum_extra(executed, "idem_conflict", "attempts")
    model_fallbacks = _sum_extra(executed, "model", "fallbacks")
    model_attempts = _sum_extra(executed, "model", "attempts")
    model_schema_rejections = _sum_extra(executed, "model", "schema_rejections")
    model_output_attempts = _sum_extra(executed, "model", "output_attempts")
    replay_matched = _sum_extra(executed, "replay", "matched_steps")
    replay_total = _sum_extra(executed, "replay", "total_steps")
    handoff_succeeded = _sum_extra(executed, "approval_handoff", "succeeded")
    handoff_attempted = _sum_extra(executed, "approval_handoff", "attempted")
    device_recovered = _sum_extra(executed, "device", "faults_recovered")
    device_injected = _sum_extra(executed, "device", "faults_injected")

    return {
        "success_rate": success_rate(passed, total),
        "policy_violation_rate": policy_violation_rate(violations, tool_executions),
        "tool_correct_rate": tool_correct_rate(correct, len(tool_calls)),
        "invalid_loop_rate": invalid_loop_rate(loop_runs, total),
        "recovery_success_rate": recovery_success_rate(recovered, attempted),
        "p95_decision_latency_ms": p95_ms(latencies),
        # WP-12 第三波新增指标
        "restart_recovery_rate": restart_recovery_rate(restart_succeeded, restart_attempted),
        "idempotency_conflict_rate": idempotency_conflict_rate(idem_conflicts, idem_attempts),
        "model_fallback_rate": model_fallback_rate(model_fallbacks, model_attempts),
        "model_schema_rejection_rate": model_schema_rejection_rate(
            model_schema_rejections, model_output_attempts
        ),
        "trace_replay_match_rate": trace_replay_match_rate(replay_matched, replay_total),
        "approval_handoff_success_rate": approval_handoff_success_rate(
            handoff_succeeded, handoff_attempted
        ),
        "device_fault_recovery_rate": device_fault_recovery_rate(device_recovered, device_injected),
        "business_success_rate": success_rate(business_ok, total),
        "definitions": dict(METRIC_DEFINITIONS),
        "denominators": {
            "scenarios": total,
            "passed_scenarios": passed,
            "tool_calls": len(tool_calls),
            "tool_calls_correct": correct,
            "tool_executions": tool_executions,
            "policy_violations": violations,
            "invalid_loop_runs": loop_runs,
            "recovery_attempted": attempted,
            "recovery_succeeded": recovered,
            # WP-12 第三波分母台账
            "restart_attempted": restart_attempted,
            "restart_succeeded": restart_succeeded,
            "idem_conflict_attempts": idem_attempts,
            "idem_conflicts_detected": idem_conflicts,
            "model_attempts": model_attempts,
            "model_output_attempts": model_output_attempts,
            "model_fallbacks": model_fallbacks,
            "model_schema_rejections": model_schema_rejections,
            "replay_total_steps": replay_total,
            "replay_matched_steps": replay_matched,
            "approval_handoff_attempted": handoff_attempted,
            "approval_handoff_succeeded": handoff_succeeded,
            "device_faults_injected": device_injected,
            "device_faults_recovered": device_recovered,
        },
    }


def _steps_of(result: ScenarioResult) -> list[dict[str, Any]]:
    """ScenarioResult.steps 的防御性读取（保持 compute_metrics 的简单形态）。"""
    return result.steps


def _must_not_execute_of(result: ScenarioResult) -> set[str]:
    """从 expected/must_not_execute 取场景禁止执行集。

    ScenarioResult 不含 must_not_execute 字段（它是规格数据），因此从
    场景规格表回查 —— 此处通过 scenarios.SCENARIOS 反查，保证与
    脚本/契约测试同源。
    """
    from scenarios import SCENARIOS

    for sc in SCENARIOS:
        if sc["id"] == result.scenario_id:
            return set(sc.get("must_not_execute", []))
    return set()
