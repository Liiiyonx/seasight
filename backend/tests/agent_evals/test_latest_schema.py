"""WP-05/WP-12 契约测试（三）：latest_v2.json schema 锁定。

验证（执行手册 3.8：GET /agents/evals/latest 由 WP-03 读取本产物）：
- 顶层字段齐全（command/date/code_version/config/evidence_level/
  sample_size/skipped_count/scenarios/metrics）。
- code_version 含 git 短哈希与内容指纹；config 含场景定义/脚本哈希。
- 指标值域合法（None 或 [0,1]；P95 非负；分母台账齐全）。
- WP-12 七项新增指标必须存在（重启恢复率/幂等冲突率/模型回退率/
  Schema 拒绝率/轨迹回放匹配率/审批交接成功率/设备故障恢复率）。
- 跳过语义：skipped 场景 passed=false、skip_reason 非空、actual 为空；
  sample_size 只统计已执行场景。
- evidence_level 纪律：必须 E1，禁止 E3/E4。
- 若 artifacts/agent_evals/latest_v2.json 已存在（评测脚本已运行），
  对其做完整 schema 校验 —— 即「端点可消费性」的契约锁定。
- v1 产物 artifacts/agent_evals/latest.json 必须保持原样（schema 1.0，
  不随 v2 覆盖）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from contract import (  # noqa: E402
    EVAL_EVIDENCE_LEVEL,
    REPORT_SCHEMA_VERSION,
    REPORT_TYPE,
    REQUIRED_ACTUAL,
    REQUIRED_CONFIG,
    REQUIRED_CODE_VERSION,
    REQUIRED_DENOMINATORS,
    REQUIRED_EXPECTED,
    REQUIRED_METRICS,
    REQUIRED_SCENARIO,
    REQUIRED_TOP_LEVEL,
    sample_report,
    validate_latest_json,
)

ROOT = Path(__file__).resolve().parent.parent.parent.parent
V2_ARTIFACT = ROOT / "artifacts" / "agent_evals" / "latest_v2.json"
V1_ARTIFACT = ROOT / "artifacts" / "agent_evals" / "latest.json"


class TestSchemaCompleteness:
    def test_schema_version_and_type(self):
        assert REPORT_SCHEMA_VERSION == "2.0"
        assert REPORT_TYPE == "agent_evals"

    def test_required_lists_are_declared(self):
        assert len(REQUIRED_TOP_LEVEL) == len(set(REQUIRED_TOP_LEVEL))
        assert "code_version" in REQUIRED_TOP_LEVEL
        assert "config" in REQUIRED_TOP_LEVEL
        assert "metrics" in REQUIRED_TOP_LEVEL
        assert "skipped_count" in REQUIRED_TOP_LEVEL
        assert len(REQUIRED_METRICS) == 13, "六项 WP-05 + 七项 WP-12 指标必须齐全"
        assert set(REQUIRED_METRICS) == {
            # WP-05 六项
            "success_rate",
            "policy_violation_rate",
            "tool_correct_rate",
            "invalid_loop_rate",
            "recovery_success_rate",
            "p95_decision_latency_ms",
            # WP-12 七项
            "restart_recovery_rate",
            "idempotency_conflict_rate",
            "model_fallback_rate",
            "model_schema_rejection_rate",
            "trace_replay_match_rate",
            "approval_handoff_success_rate",
            "device_fault_recovery_rate",
        }
        assert len(REQUIRED_DENOMINATORS) == 23
        for key in (
            "restart_attempted",
            "idem_conflict_attempts",
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
        ):
            assert key in REQUIRED_DENOMINATORS, f"缺少 WP-12 分母 {key}"

    def test_sample_report_is_valid(self):
        errors = validate_latest_json(sample_report())
        assert errors == [], f"样例报告应通过 schema：{errors}"

    def test_sample_report_has_skipped_scenario_semantics(self):
        report = sample_report()
        assert report["sample_size"] == 1
        assert report["skipped_count"] == 1
        skipped = [sc for sc in report["scenarios"] if sc["skipped"]]
        assert len(skipped) == 1
        assert skipped[0]["passed"] is False
        assert skipped[0]["skip_reason"]
        assert skipped[0]["actual"]["status"] is None


class TestSchemaRejection:
    def _base(self):
        return sample_report()

    def test_rejects_missing_top_level_key(self):
        report = self._base()
        del report["metrics"]
        assert "缺少顶层字段 metrics" in validate_latest_json(report)

    def test_rejects_missing_code_version_fields(self):
        report = self._base()
        del report["code_version"]["fingerprint"]
        assert any("code_version" in e and "fingerprint" in e for e in validate_latest_json(report))

    def test_rejects_missing_config_hash(self):
        report = self._base()
        del report["config"]["hash"]
        assert any("config" in e and "hash" in e for e in validate_latest_json(report))

    def test_rejects_bad_fingerprint(self):
        report = self._base()
        report["code_version"]["fingerprint"] = "not-a-hash"
        assert any("fingerprint" in e for e in validate_latest_json(report))

    def test_rejects_metric_out_of_domain(self):
        report = self._base()
        report["metrics"]["success_rate"] = 1.5
        assert any("success_rate" in e for e in validate_latest_json(report))

    def test_rejects_negative_p95(self):
        report = self._base()
        report["metrics"]["p95_decision_latency_ms"] = -1
        assert any("p95" in e for e in validate_latest_json(report))

    def test_rejects_negative_sample_size(self):
        report = self._base()
        report["sample_size"] = -1
        assert any("sample_size" in e for e in validate_latest_json(report))

    def test_rejects_sample_size_mismatch(self):
        report = self._base()
        report["sample_size"] = 99
        assert any("sample_size" in e and "不一致" in e for e in validate_latest_json(report))

    def test_rejects_non_e1_evidence(self):
        """纪律：本评测全部为 E1，禁止写成 E3/E4。"""
        report = self._base()
        report["evidence_level"] = "E3"
        assert any("evidence_level" in e for e in validate_latest_json(report))
        report = self._base()
        report["scenarios"][0]["evidence_level"] = "E4"
        assert any("evidence_level" in e for e in validate_latest_json(report))

    def test_rejects_network_access_true(self):
        report = self._base()
        report["environment"]["network_access"] = True
        assert any("network_access" in e for e in validate_latest_json(report))

    def test_rejects_unknown_error_code(self):
        report = self._base()
        report["scenarios"][0]["expected"]["error_code"] = "not_a_frozen_code"
        assert any("error_code" in e for e in validate_latest_json(report))

    def test_rejects_succeeded_with_error_code(self):
        report = self._base()
        report["scenarios"][0]["expected"]["error_code"] = "tool_failed"
        assert any("成功终态不允许带错误码" in e for e in validate_latest_json(report))

    def test_rejects_non_terminal_expected_status(self):
        report = self._base()
        report["scenarios"][0]["expected"]["status"] = "executing"
        assert any("必须是终态" in e for e in validate_latest_json(report))

    def test_rejects_missing_denominator(self):
        report = self._base()
        del report["metrics"]["denominators"]["recovery_attempted"]
        assert any("recovery_attempted" in e for e in validate_latest_json(report))

    def test_rejects_duplicate_scenario_id(self):
        report = self._base()
        report["scenarios"].append(dict(report["scenarios"][0]))
        report["sample_size"] = 2
        assert any("重复" in e for e in validate_latest_json(report))

    def test_rejects_non_object(self):
        assert validate_latest_json([1, 2]) != []
        assert validate_latest_json("x") != []

    # ---- WP-12 跳过语义 + 新增指标 ----

    def test_rejects_missing_wave3_metric(self):
        report = self._base()
        del report["metrics"]["device_fault_recovery_rate"]
        assert any("device_fault_recovery_rate" in e for e in validate_latest_json(report))

    def test_rejects_missing_wave3_denominator(self):
        report = self._base()
        del report["metrics"]["denominators"]["replay_total_steps"]
        assert any("replay_total_steps" in e for e in validate_latest_json(report))

    def test_rejects_missing_skipped_count(self):
        report = self._base()
        del report["skipped_count"]
        assert "缺少顶层字段 skipped_count" in validate_latest_json(report)

    def test_rejects_skipped_count_mismatch(self):
        report = self._base()
        report["skipped_count"] = 99
        assert any("skipped_count" in e and "不一致" in e for e in validate_latest_json(report))

    def test_rejects_skipped_without_reason(self):
        report = self._base()
        report["scenarios"][1]["skip_reason"] = None
        assert any("skip_reason" in e for e in validate_latest_json(report))

    def test_rejects_skipped_with_terminal_actual_status(self):
        report = self._base()
        report["scenarios"][1]["actual"]["status"] = "succeeded"
        assert any("跳过场景" in e and "actual.status" in e for e in validate_latest_json(report))

    def test_rejects_non_skipped_with_skip_reason(self):
        report = self._base()
        report["scenarios"][0]["skip_reason"] = "不应有"
        assert any("非跳过场景 skip_reason 必须为 null" in e for e in validate_latest_json(report))

    def test_rejects_wave3_metric_out_of_domain(self):
        report = self._base()
        report["metrics"]["model_fallback_rate"] = 1.5
        assert any("model_fallback_rate" in e for e in validate_latest_json(report))

    def test_rejects_sample_size_not_matching_executed(self):
        report = self._base()
        report["sample_size"] = 2  # 只有 1 个非跳过场景
        assert any("sample_size" in e and "已执行场景数" in e for e in validate_latest_json(report))


class TestArtifactLatestJson:
    """评测产物契约锁定：latest_v2.json 必须能被 /evals/latest 端点消费。"""

    def test_artifact_present_and_schema_valid(self):
        if not V2_ARTIFACT.exists():
            pytest.skip("artifacts/agent_evals/latest_v2.json 不存在（先运行 scripts/run_agent_evals.py）")
        report = json.loads(V2_ARTIFACT.read_text(encoding="utf-8"))
        errors = validate_latest_json(report)
        assert errors == [], f"latest_v2.json 违反 schema：{errors}"

    def test_artifact_code_version_and_config_hash_present(self):
        if not V2_ARTIFACT.exists():
            pytest.skip("artifacts/agent_evals/latest_v2.json 不存在")
        report = json.loads(V2_ARTIFACT.read_text(encoding="utf-8"))
        cv = report["code_version"]
        assert cv["source"] == "git"
        assert len(cv["value"]) >= 7, "代码版本应为 git HEAD 短哈希（至少 7 位）"
        assert len(cv["fingerprint"]) == 64
        assert len(report["config"]["hash"]) == 64
        assert report["evidence_level"] == EVAL_EVIDENCE_LEVEL

    def test_artifact_scenario_details_and_metrics(self):
        if not V2_ARTIFACT.exists():
            pytest.skip("artifacts/agent_evals/latest_v2.json 不存在")
        report = json.loads(V2_ARTIFACT.read_text(encoding="utf-8"))
        assert report["schema_version"] == "2.0"
        assert report["sample_size"] == len([sc for sc in report["scenarios"] if not sc["skipped"]]) >= 20
        for sc in report["scenarios"]:
            for key in REQUIRED_SCENARIO:
                assert key in sc, f"场景 {sc.get('id')} 缺少 {key}"
            for key in REQUIRED_EXPECTED:
                assert key in sc["expected"]
            for key in REQUIRED_ACTUAL:
                assert key in sc["actual"]
            if sc["skipped"]:
                assert sc["passed"] is False
                assert sc["skip_reason"]
                assert sc["actual"]["status"] is None
        for key in REQUIRED_METRICS:
            assert key in report["metrics"]
        for key in REQUIRED_DENOMINATORS:
            assert key in report["metrics"]["denominators"]

    def test_artifact_passed_scenarios_all_pass(self):
        """固定场景集当前应全部通过（内核冻结、期望与 WP-01/WP-10/WP-11 行为一致）。"""
        if not V2_ARTIFACT.exists():
            pytest.skip("artifacts/agent_evals/latest_v2.json 不存在")
        report = json.loads(V2_ARTIFACT.read_text(encoding="utf-8"))
        failed = [sc["id"] for sc in report["scenarios"] if not sc["skipped"] and not sc["passed"]]
        assert not failed, f"固定场景存在未通过项：{failed}"

    def test_artifact_covers_wave3_scenarios(self):
        """冻结的 10 个第三波场景必须全部出现在 v2 产物中。"""
        if not V2_ARTIFACT.exists():
            pytest.skip("artifacts/agent_evals/latest_v2.json 不存在")
        from scenarios import REQUIRED_WAVE3_SCENARIO_IDS

        report = json.loads(V2_ARTIFACT.read_text(encoding="utf-8"))
        ids = {sc["id"] for sc in report["scenarios"]}
        missing = [wid for wid in REQUIRED_WAVE3_SCENARIO_IDS if wid not in ids]
        assert not missing, f"latest_v2.json 缺少 WP-12 冻结场景：{missing}"


class TestArtifactV1Preserved:
    """v1 产物 latest.json 必须保持原样（schema 1.0），不随 v2 覆盖。"""

    def test_v1_artifact_untouched_if_present(self):
        if not V1_ARTIFACT.exists():
            pytest.skip("artifacts/agent_evals/latest.json 不存在（v1 历史产物）")
        report = json.loads(V1_ARTIFACT.read_text(encoding="utf-8"))
        assert report["schema_version"] == "1.0", (
            "v1 latest.json 必须是 schema 1.0；若被覆盖为 2.0 说明 v1 结果被破坏"
        )
        # v1 只含 WP-05 六项指标，不含 WP-12 新增指标
        assert "device_fault_recovery_rate" not in report["metrics"]
        assert "skipped_count" not in report


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
