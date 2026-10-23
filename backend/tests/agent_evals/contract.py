"""WP-05/WP-12 评测产物 latest_v2.json 的 schema 契约（唯一真源）。

定位（执行手册 3.8：GET /agents/evals/latest 由 WP-03 在 agents 路由读取
本产物）：本模块锁定「/evals/latest 端点可消费的 JSON 结构」，供
scripts/run_agent_evals.py 产出前自校验，并由
backend/tests/agent_evals/test_latest_schema.py 做契约测试锁定。

版本：v1.0（WP-05，六项聚合指标）→ v2.0（WP-12，追加七项聚合指标与
跳过场景语义）。v1 产物 artifacts/agent_evals/latest.json 保持原样不覆盖，
作为历史结果保留；v2 产物写入 artifacts/agent_evals/latest_v2.json。

纪律（总控 E0-E4）：本评测全部为 E1（内部实现 / 确定性仿真证据），
`validate_latest_json` 强制 evidence_level == "E1"，禁止写成 E3/E4。

字段冻结说明：新增字段必须先改本模块 + 契约测试，再改脚本，防止
WP-03 消费端与评测端静默漂移。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# ---- 路径引导：本模块会被 scripts/ 与 pytest 两种方式导入 ----
_EVAL_DIR = Path(__file__).resolve().parent
_BACKEND = _EVAL_DIR.parent.parent  # backend/tests/agent_evals -> backend
for _p in (_BACKEND, _EVAL_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# 冻结错误码 / 终态（与 WP-01 内核同源，禁止出现未知值）
from app.services.agents import ERROR_CODES, TERMINAL_STATUSES  # noqa: E402

REPORT_SCHEMA_VERSION = "2.0"
REPORT_TYPE = "agent_evals"

# ---- 顶层必填字段（/evals/latest 端点消费方依赖的骨架） ----
REQUIRED_TOP_LEVEL = [
    "schema_version",
    "report_type",
    "command",
    "date",
    "code_version",
    "config",
    "evidence_level",
    "environment",
    "sample_size",
    "skipped_count",
    "scenarios",
    "metrics",
]

REQUIRED_CODE_VERSION = ["source", "value", "fingerprint", "note"]
REQUIRED_CONFIG = ["hash", "scenario_count", "scenario_ids", "files"]
REQUIRED_SCENARIO = [
    "id",
    "name",
    "description",
    "evidence_level",
    "expected",
    "passed",
    "skipped",
    "skip_reason",
    "actual",
    "notes",
]
REQUIRED_EXPECTED = ["status", "error_code", "shape"]
REQUIRED_ACTUAL = [
    "status",
    "error_code",
    "termination_reason",
    "step_count",
    "step_types",
    "tool_executions",
    "decision_latency_ms",
]

# WP-05 六项聚合指标 + WP-12 七项聚合指标（docs/agent-program-wave3.md 3.3）
# + 说明性业务成功率
REQUIRED_METRICS = [
    "success_rate",
    "policy_violation_rate",
    "tool_correct_rate",
    "invalid_loop_rate",
    "recovery_success_rate",
    "p95_decision_latency_ms",
    "restart_recovery_rate",
    "idempotency_conflict_rate",
    "model_fallback_rate",
    "model_schema_rejection_rate",
    "trace_replay_match_rate",
    "approval_handoff_success_rate",
    "device_fault_recovery_rate",
]
INFORMATIONAL_METRICS = ["business_success_rate"]
REQUIRED_METRIC_DEFINITIONS = list(REQUIRED_METRICS)
REQUIRED_DENOMINATORS = [
    "scenarios",
    "passed_scenarios",
    "tool_calls",
    "tool_calls_correct",
    "tool_executions",
    "policy_violations",
    "invalid_loop_runs",
    "recovery_attempted",
    "recovery_succeeded",
    # WP-12 第三波分母台账
    "restart_attempted",
    "restart_succeeded",
    "idem_conflict_attempts",
    "idem_conflicts_detected",
    "model_attempts",
    "model_output_attempts",
    "model_fallbacks",
    "model_schema_rejections",
    "replay_total_steps",
    "replay_matched_steps",
    "approval_handoff_attempted",
    "approval_handoff_succeeded",
    "device_faults_injected",
    "device_faults_recovered",
]

VALID_EVIDENCE_LEVELS = ("E0", "E1", "E2", "E3", "E4")

# 本评测纪律：全部为 E1（确定性仿真证据），禁止 E3/E4
EVAL_EVIDENCE_LEVEL = "E1"

_HEX64 = set("0123456789abcdef")


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _HEX64 for ch in value)
    )


def _is_rate(value: Any) -> bool:
    """指标值域：null（分母为零语义）或 [0,1] 闭区间。"""
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0


def validate_latest_json(obj: Any) -> list[str]:
    """校验评测报告对象，返回错误列表；空列表表示通过。

    同时被脚本（产出前自校验）与契约测试（锁定 schema）复用。
    """
    errors: list[str] = []
    if not isinstance(obj, dict):
        return ["评测报告必须是 JSON 对象"]

    for key in REQUIRED_TOP_LEVEL:
        if key not in obj:
            errors.append(f"缺少顶层字段 {key}")
    if "schema_version" in obj and obj["schema_version"] != REPORT_SCHEMA_VERSION:
        errors.append(f"schema_version 必须为 {REPORT_SCHEMA_VERSION}，实际 {obj['schema_version']!r}")
    if "report_type" in obj and obj["report_type"] != REPORT_TYPE:
        errors.append(f"report_type 必须为 {REPORT_TYPE!r}，实际 {obj['report_type']!r}")
    if "command" in obj and not isinstance(obj["command"], str):
        errors.append("command 必须是字符串")
    if "date" in obj and not isinstance(obj["date"], str):
        errors.append("date 必须是字符串（ISO 8601）")

    # ---- code_version：代码版本（git HEAD 短哈希 + 内容指纹） ----
    cv = obj.get("code_version")
    if not isinstance(cv, dict):
        errors.append("code_version 必须是对象")
    else:
        for key in REQUIRED_CODE_VERSION:
            if key not in cv:
                errors.append(f"code_version 缺少字段 {key}")
        if isinstance(cv.get("value"), str) and not cv["value"]:
            errors.append("code_version.value 不能为空")
        if "fingerprint" in cv and not _is_hex64(cv["fingerprint"]):
            errors.append("code_version.fingerprint 必须是 64 位十六进制 sha256")

    # ---- config：配置哈希（场景定义/脚本指纹） ----
    cfg = obj.get("config")
    if not isinstance(cfg, dict):
        errors.append("config 必须是对象")
    else:
        for key in REQUIRED_CONFIG:
            if key not in cfg:
                errors.append(f"config 缺少字段 {key}")
        if "hash" in cfg and not _is_hex64(cfg["hash"]):
            errors.append("config.hash 必须是 64 位十六进制 sha256")
        ids = cfg.get("scenario_ids")
        if ids is not None and not isinstance(ids, list):
            errors.append("config.scenario_ids 必须是数组")
        if ids is not None and isinstance(ids, list) and any(not isinstance(i, str) for i in ids):
            errors.append("config.scenario_ids 必须全部是字符串")
        if "scenario_count" in cfg:
            n = cfg["scenario_count"]
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                errors.append("config.scenario_count 必须是非负整数")

    # ---- evidence_level：E0-E4 纪律 ----
    ev = obj.get("evidence_level")
    if ev != EVAL_EVIDENCE_LEVEL:
        errors.append(
            f"evidence_level 必须为 E1（本评测全部为确定性仿真证据），实际 {ev!r}"
        )

    # ---- environment：离线声明 ----
    env = obj.get("environment")
    if not isinstance(env, dict):
        errors.append("environment 必须是对象")
    else:
        for key in ("network_access", "model_access", "device_access"):
            if key not in env:
                errors.append(f"environment 缺少字段 {key}")
            elif env[key] is not False:
                errors.append(f"environment.{key} 必须为 false（评测禁止访问真实模型/网络/设备）")

    # ---- sample_size（执行数）与 scenarios（含跳过语义） ----
    n = obj.get("sample_size")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        errors.append("sample_size 必须是非负整数")
    skipped_count = obj.get("skipped_count")
    if not isinstance(skipped_count, int) or isinstance(skipped_count, bool) or skipped_count < 0:
        errors.append("skipped_count 必须是非负整数")
    scenarios = obj.get("scenarios")
    if not isinstance(scenarios, list):
        errors.append("scenarios 必须是数组")
    else:
        executed = [sc for sc in scenarios if not sc.get("skipped")]
        skipped = [sc for sc in scenarios if sc.get("skipped")]
        if isinstance(n, int) and not isinstance(n, bool) and n >= 0 and len(executed) != n:
            errors.append(f"sample_size={n} 与已执行场景数 {len(executed)} 不一致")
        if isinstance(skipped_count, int) and not isinstance(skipped_count, bool) and skipped_count >= 0 and len(skipped) != skipped_count:
            errors.append(f"skipped_count={skipped_count} 与跳过场景数 {len(skipped)} 不一致")
        seen: set[str] = set()
        for i, sc in enumerate(scenarios):
            for key in REQUIRED_SCENARIO:
                if key not in sc:
                    errors.append(f"scenarios[{i}] 缺少字段 {key}")
            if not isinstance(sc.get("id"), str) or not sc["id"]:
                errors.append(f"scenarios[{i}].id 必须是非空字符串")
            elif sc["id"] in seen:
                errors.append(f"scenarios[{i}].id 重复：{sc['id']}")
            seen.add(sc["id"])
            if sc.get("evidence_level") != EVAL_EVIDENCE_LEVEL:
                errors.append(f"scenarios[{i}].evidence_level 必须为 E1")
            if not isinstance(sc.get("passed"), bool):
                errors.append(f"scenarios[{i}].passed 必须是布尔值")
            is_skipped = sc.get("skipped") is True
            if "skipped" in sc and not isinstance(sc["skipped"], bool):
                errors.append(f"scenarios[{i}].skipped 必须是布尔值")
            if is_skipped:
                if sc.get("passed") is not False:
                    errors.append(f"scenarios[{i}] 跳过场景 passed 必须为 false")
                if not isinstance(sc.get("skip_reason"), str) or not sc["skip_reason"]:
                    errors.append(f"scenarios[{i}] 跳过场景必须有非空 skip_reason")
            else:
                if sc.get("skip_reason") is not None:
                    errors.append(f"scenarios[{i}] 非跳过场景 skip_reason 必须为 null")
            exp = sc.get("expected")
            if not isinstance(exp, dict):
                errors.append(f"scenarios[{i}].expected 必须是对象")
            else:
                for key in REQUIRED_EXPECTED:
                    if key not in exp:
                        errors.append(f"scenarios[{i}].expected 缺少字段 {key}")
                if exp.get("status") not in TERMINAL_STATUSES:
                    errors.append(
                        f"scenarios[{i}].expected.status 必须是终态 {sorted(TERMINAL_STATUSES)}，"
                        f"实际 {exp.get('status')!r}"
                    )
                ec = exp.get("error_code")
                if ec is not None and ec not in ERROR_CODES:
                    errors.append(f"scenarios[{i}].expected.error_code 不是冻结错误码：{ec!r}")
                if exp.get("status") == "succeeded" and ec is not None:
                    errors.append(f"scenarios[{i}].expected 成功终态不允许带错误码：{ec!r}")
                if not isinstance(exp.get("shape"), str) or not exp["shape"]:
                    errors.append(f"scenarios[{i}].expected.shape 必须是非空字符串")
            act = sc.get("actual")
            if not isinstance(act, dict):
                errors.append(f"scenarios[{i}].actual 必须是对象")
            else:
                for key in REQUIRED_ACTUAL:
                    if key not in act:
                        errors.append(f"scenarios[{i}].actual 缺少字段 {key}")
                if is_skipped:
                    # 跳过场景没有真实执行结果：status 等允许为 null
                    if act.get("status") is not None:
                        errors.append(f"scenarios[{i}] 跳过场景 actual.status 必须为 null")
                    if act.get("error_code") is not None:
                        errors.append(f"scenarios[{i}] 跳过场景 actual.error_code 必须为 null")
                else:
                    if act.get("status") not in TERMINAL_STATUSES:
                        errors.append(
                            f"scenarios[{i}].actual.status 必须是终态 {sorted(TERMINAL_STATUSES)}，"
                            f"实际 {act.get('status')!r}"
                        )
                    ec = act.get("error_code")
                    if ec is not None and ec not in ERROR_CODES:
                        errors.append(f"scenarios[{i}].actual.error_code 不是冻结错误码：{ec!r}")
                    if act.get("status") == "succeeded" and ec is not None:
                        errors.append(f"scenarios[{i}].actual 成功终态不允许带错误码：{ec!r}")
                    if not isinstance(act.get("step_count"), int) or isinstance(act.get("step_count"), bool):
                        errors.append(f"scenarios[{i}].actual.step_count 必须是整数")
                    if not isinstance(act.get("step_types"), list):
                        errors.append(f"scenarios[{i}].actual.step_types 必须是数组")
                    if not isinstance(act.get("tool_executions"), dict):
                        errors.append(f"scenarios[{i}].actual.tool_executions 必须是对象")
                    if not isinstance(act.get("decision_latency_ms"), (int, float)) or isinstance(act.get("decision_latency_ms"), bool):
                        errors.append(f"scenarios[{i}].actual.decision_latency_ms 必须是数字")
                    elif act["decision_latency_ms"] < 0:
                        errors.append(f"scenarios[{i}].actual.decision_latency_ms 不能为负")
            if not isinstance(sc.get("notes"), list):
                errors.append(f"scenarios[{i}].notes 必须是数组")

    # ---- metrics：六项 + 七项聚合指标 ----
    metrics = obj.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("metrics 必须是对象")
    else:
        for key in REQUIRED_METRICS:
            if key not in metrics:
                errors.append(f"metrics 缺少聚合指标 {key}")
        for key in REQUIRED_METRICS + INFORMATIONAL_METRICS:
            if key in metrics and not _is_rate(metrics[key]):
                errors.append(f"metrics.{key} 值域非法（必须为 null 或 [0,1]）：{metrics[key]!r}")
        p95 = metrics.get("p95_decision_latency_ms")
        if p95 is not None and (not isinstance(p95, (int, float)) or isinstance(p95, bool) or p95 < 0):
            errors.append(f"metrics.p95_decision_latency_ms 值域非法：{p95!r}")
        defs = metrics.get("definitions")
        if not isinstance(defs, dict):
            errors.append("metrics.definitions 必须是对象（口径文档）")
        else:
            for key in REQUIRED_METRIC_DEFINITIONS:
                if key not in defs:
                    errors.append(f"metrics.definitions 缺少 {key} 的口径")
                elif not isinstance(defs[key], str) or not defs[key]:
                    errors.append(f"metrics.definitions.{key} 必须是非空字符串")
        dens = metrics.get("denominators")
        if not isinstance(dens, dict):
            errors.append("metrics.denominators 必须是对象")
        else:
            for key in REQUIRED_DENOMINATORS:
                if key not in dens:
                    errors.append(f"metrics.denominators 缺少 {key}")
                elif not isinstance(dens[key], int) or isinstance(dens[key], bool) or dens[key] < 0:
                    errors.append(f"metrics.denominators.{key} 必须是非负整数")

    return errors


def sample_report() -> dict[str, Any]:
    """构造一份结构合法的样例报告（契约测试与脚本自校验复用）。"""
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "command": "python scripts/run_agent_evals.py",
        "date": "2026-09-19T00:00:00+08:00",
        "code_version": {
            "source": "git",
            "value": "0000000",
            "fingerprint": "0" * 64,
            "note": "样例",
        },
        "config": {
            "hash": "0" * 64,
            "scenario_count": 2,
            "scenario_ids": ["sample_scenario", "sample_skipped"],
            "files": ["backend/tests/agent_evals/scenarios.py"],
        },
        "evidence_level": "E1",
        "environment": {
            "network_access": False,
            "model_access": False,
            "device_access": False,
            "runtime_backend": "in-memory",
            "persistent_backend": "sqlite-memory",
        },
        "sample_size": 1,
        "skipped_count": 1,
        "scenarios": [
            {
                "id": "sample_scenario",
                "name": "样例场景",
                "description": "仅供 schema 契约测试使用",
                "evidence_level": "E1",
                "expected": {"status": "succeeded", "error_code": None, "shape": "plan → terminal"},
                "passed": True,
                "skipped": False,
                "skip_reason": None,
                "actual": {
                    "status": "succeeded",
                    "error_code": None,
                    "termination_reason": "succeeded",
                    "step_count": 1,
                    "step_types": ["plan"],
                    "tool_executions": {},
                    "decision_latency_ms": 0.0,
                },
                "notes": [],
            },
            {
                "id": "sample_skipped",
                "name": "样例跳过场景",
                "description": "依赖接口缺失时的明确跳过语义",
                "evidence_level": "E1",
                "expected": {"status": "succeeded", "error_code": None, "shape": "plan → terminal"},
                "passed": False,
                "skipped": True,
                "skip_reason": "app.services.agents.model_adapter 不可用：样例原因",
                "actual": {
                    "status": None,
                    "error_code": None,
                    "termination_reason": None,
                    "step_count": 0,
                    "step_types": [],
                    "tool_executions": {},
                    "decision_latency_ms": None,
                },
                "notes": ["场景跳过：app.services.agents.model_adapter 不可用：样例原因"],
            },
        ],
        "metrics": {
            "success_rate": 1.0,
            "policy_violation_rate": None,
            "tool_correct_rate": None,
            "invalid_loop_rate": 0.0,
            "recovery_success_rate": None,
            "p95_decision_latency_ms": 0.0,
            "restart_recovery_rate": None,
            "idempotency_conflict_rate": None,
            "model_fallback_rate": None,
            "model_schema_rejection_rate": None,
            "trace_replay_match_rate": None,
            "approval_handoff_success_rate": None,
            "device_fault_recovery_rate": None,
            "business_success_rate": 1.0,
            "definitions": {
                "success_rate": "期望结果达成的场景数 / 场景总数",
                "policy_violation_rate": "策略违规执行次数 / 工具实际执行次数",
                "tool_correct_rate": "参数/权限/幂等均正确的工具调用数 / 工具调用步骤总数",
                "invalid_loop_rate": "超步数或重复无进展步骤的 run 数 / run 总数",
                "recovery_success_rate": "可恢复异常中成功恢复的 run 数 / 尝试恢复的 run 数",
                "p95_decision_latency_ms": "从触发到首个可执行计划产出的耗时（毫秒）P95",
                "restart_recovery_rate": "重启后成功续跑的 run 数 / 需要重启续跑的 run 数",
                "idempotency_conflict_rate": "幂等键/乐观锁冲突被正确拒绝的尝试数 / 冲突写入尝试总数",
                "model_fallback_rate": "模型规划回退规则模式的次数 / 模型规划尝试总次数",
                "model_schema_rejection_rate": "模型输出被严格校验链拒绝的回退次数 / 模型实际返回输出的尝试次数",
                "trace_replay_match_rate": "回放与原始轨迹匹配的步骤数 / 回放总步骤数",
                "approval_handoff_success_rate": "跨角色审批交接成功完成的次数 / 需要跨角色交接的审批次数",
                "device_fault_recovery_rate": "成功恢复的注入故障数 / 注入故障总数（无故障时 null）",
            },
            "denominators": {
                "scenarios": 1,
                "passed_scenarios": 1,
                "tool_calls": 0,
                "tool_calls_correct": 0,
                "tool_executions": 0,
                "policy_violations": 0,
                "invalid_loop_runs": 0,
                "recovery_attempted": 0,
                "recovery_succeeded": 0,
                "restart_attempted": 0,
                "restart_succeeded": 0,
                "idem_conflict_attempts": 0,
                "idem_conflicts_detected": 0,
                "model_attempts": 0,
                "model_output_attempts": 0,
                "model_fallbacks": 0,
                "model_schema_rejections": 0,
                "replay_total_steps": 0,
                "replay_matched_steps": 0,
                "approval_handoff_attempted": 0,
                "approval_handoff_succeeded": 0,
                "device_faults_injected": 0,
                "device_faults_recovered": 0,
            },
        },
    }
