"""WP-05/WP-12 契约测试（一）：固定场景集定义完整性。

验证：
- 每个场景都有期望结果（终态 status + error_code + 轨迹形状）。
- 场景 ID 唯一、元数据齐全、evidence_level 全部为 E1（禁止 E3/E4）。
- 期望错误码 ∈ 冻结错误码；成功终态不允许带错误码。
- 必须覆盖手册第 4 节 WP-01 测试场景的可评测化清单。
- 必须覆盖第三波冻结场景清单（docs/agent-program-wave3.md 3.3）。
- 必须覆盖第四波冻结场景清单（多角色研判门禁 + 跨 run 经验闭环）。
- 每个场景规格都有对应执行器，反之亦然。

纯逻辑测试：不执行任何场景（执行与断言在脚本与指标测试中）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from app.services.agents import ERROR_CODES, TERMINAL_STATUSES  # noqa: E402
from scenarios import (  # noqa: E402
    REQUIRED_SCENARIO_IDS,
    REQUIRED_WAVE3_SCENARIO_IDS,
    REQUIRED_WAVE4_SCENARIO_IDS,
    SCENARIO_RUNNERS,
    SCENARIOS,
)


class TestScenarioSetCompleteness:
    def test_scenarios_non_empty_and_ids_unique(self):
        assert SCENARIOS, "固定场景集不能为空"
        ids = [sc["id"] for sc in SCENARIOS]
        assert len(ids) == len(set(ids)), f"场景 ID 重复：{ids}"

    def test_required_scenario_coverage(self):
        """手册第 4 节 WP-01 测试场景的可评测化清单必须全部存在。"""
        ids = {sc["id"] for sc in SCENARIOS}
        missing = [i for i in REQUIRED_SCENARIO_IDS if i not in ids]
        assert not missing, f"缺少固定场景：{missing}"

    def test_every_scenario_has_expected_terminal_status(self):
        for sc in SCENARIOS:
            expected = sc.get("expected")
            assert expected is not None, f"{sc['id']} 缺少 expected"
            status = expected.get("status")
            assert status in TERMINAL_STATUSES, (
                f"{sc['id']}.expected.status 必须是终态 {sorted(TERMINAL_STATUSES)}，实际 {status!r}"
            )

    def test_expected_error_code_is_frozen(self):
        for sc in SCENARIOS:
            ec = sc["expected"].get("error_code")
            if ec is None:
                continue
            assert ec in ERROR_CODES, f"{sc['id']} 期望错误码不在冻结清单：{ec!r}"
            assert sc["expected"]["status"] != "succeeded", (
                f"{sc['id']} 成功终态不允许带错误码 {ec!r}"
            )

    def test_expected_shape_present(self):
        for sc in SCENARIOS:
            shape = sc["expected"].get("shape")
            assert isinstance(shape, str) and shape, f"{sc['id']} 缺少轨迹形状期望"

    def test_metadata_present(self):
        for sc in SCENARIOS:
            assert sc.get("name"), f"{sc['id']} 缺少 name"
            assert sc.get("description"), f"{sc['id']} 缺少 description"

    def test_all_scenarios_evidence_level_e1(self):
        """E0-E4 纪律：本评测全部为 E1，禁止写成 E3/E4。"""
        for sc in SCENARIOS:
            assert sc.get("evidence_level") == "E1", (
                f"{sc['id']} evidence_level 必须为 E1（确定性仿真证据），实际 {sc.get('evidence_level')!r}"
            )

    def test_must_not_execute_is_string_tuple(self):
        for sc in SCENARIOS:
            blocked = sc.get("must_not_execute", [])
            assert isinstance(blocked, (list, tuple)), f"{sc['id']}.must_not_execute 必须是列表"
            assert all(isinstance(t, str) and t for t in blocked), (
                f"{sc['id']}.must_not_execute 必须全部是非空字符串"
            )

    def test_every_spec_has_runner_and_vice_versa(self):
        spec_ids = {sc["id"] for sc in SCENARIOS}
        runner_ids = set(SCENARIO_RUNNERS)
        assert spec_ids == runner_ids, (
            f"规格与执行器不一致：仅规格 {spec_ids - runner_ids}，仅执行器 {runner_ids - spec_ids}"
        )

    def test_error_code_scenarios_cover_frozen_error_codes_used(self):
        """负向场景覆盖错误码清单的确定性分支（至少覆盖常用错误码）。"""
        covered = {sc["expected"]["error_code"] for sc in SCENARIOS if sc["expected"]["error_code"]}
        required = {
            "no_robot_available",
            "policy_denied",
            "approval_rejected",
            "approval_timeout",
            "tool_timeout",
            "max_steps_exceeded",
            "invalid_tool_output",
        }
        missing = required - covered
        assert not missing, f"固定场景未覆盖错误码：{sorted(missing)}"


class TestWave3ScenarioSetCompleteness:
    """WP-12 第三波冻结场景（docs/agent-program-wave3.md 3.3，只追加不得替换）。"""

    def test_wave3_frozen_ids_all_present_and_unique(self):
        ids = [sc["id"] for sc in SCENARIOS]
        for wid in REQUIRED_WAVE3_SCENARIO_IDS:
            assert wid in ids, f"缺少 WP-12 冻结场景：{wid}"
        assert len(REQUIRED_WAVE3_SCENARIO_IDS) == len(set(REQUIRED_WAVE3_SCENARIO_IDS))
        assert len(ids) == len(set(ids)), f"场景 ID 重复：{ids}"

    def test_wave3_does_not_replace_v1_scenarios(self):
        """原 13 个场景的 ID 与语义必须保留，不得被新场景替换。"""
        ids = {sc["id"] for sc in SCENARIOS}
        for vid in REQUIRED_SCENARIO_IDS + ["approval_approved", "replan_recovery_success", "invalid_tool_output"]:
            assert vid in ids, f"既有场景被移除：{vid}"
        for sc in SCENARIOS:
            if sc["id"] in REQUIRED_SCENARIO_IDS + [
                "approval_approved", "replan_recovery_success", "invalid_tool_output",
            ]:
                assert sc.get("description"), f"{sc['id']} 语义不得被清空"

    def test_wave3_scenarios_evidence_level_e1(self):
        for sc in SCENARIOS:
            if sc["id"] in REQUIRED_WAVE3_SCENARIO_IDS:
                assert sc.get("evidence_level") == "E1", f"{sc['id']} 必须 E1"

    def test_wave3_specs_have_runners_and_judges(self):
        spec_ids = {sc["id"] for sc in SCENARIOS}
        runner_ids = set(SCENARIO_RUNNERS)
        assert spec_ids == runner_ids, (
            f"规格与执行器不一致：仅规格 {spec_ids - runner_ids}，仅执行器 {runner_ids - spec_ids}"
        )
        from scenarios import JUDGES

        assert set(JUDGES) == spec_ids, "每个场景都必须有判定函数"


class TestWave4ScenarioSetCompleteness:
    """第四波冻结场景（多角色研判门禁 + 跨 run 经验闭环，只追加不得替换）。

    与 wave3 同构：冻结 ID 唯一、都在 SCENARIOS 里、字段齐全、runner/judge 齐全。
    新增能力如果没有对应的固定场景，就只有演示没有回归防线。
    """

    def test_wave4_frozen_ids_all_present_and_unique(self):
        ids = [sc["id"] for sc in SCENARIOS]
        for wid in REQUIRED_WAVE4_SCENARIO_IDS:
            assert wid in ids, f"缺少第四波冻结场景：{wid}"
        assert len(REQUIRED_WAVE4_SCENARIO_IDS) == len(set(REQUIRED_WAVE4_SCENARIO_IDS))
        assert len(ids) == len(set(ids)), f"场景 ID 重复：{ids}"

    def test_wave4_does_not_replace_earlier_scenarios(self):
        """前 23 个场景的 ID 必须保留，不得被新场景替换。"""
        ids = {sc["id"] for sc in SCENARIOS}
        for vid in REQUIRED_SCENARIO_IDS + REQUIRED_WAVE3_SCENARIO_IDS + [
            "approval_approved", "replan_recovery_success", "invalid_tool_output",
        ]:
            assert vid in ids, f"既有场景被移除：{vid}"

    def test_wave4_scenarios_evidence_level_e1(self):
        for sc in SCENARIOS:
            if sc["id"] in REQUIRED_WAVE4_SCENARIO_IDS:
                assert sc.get("evidence_level") == "E1", f"{sc['id']} 必须 E1"

    def test_wave4_metadata_present(self):
        """新增能力必须自带 name/description/expected/shape（不得空壳占位）。"""
        for sc in SCENARIOS:
            if sc["id"] not in REQUIRED_WAVE4_SCENARIO_IDS:
                continue
            assert sc.get("name"), f"{sc['id']} 缺少 name"
            assert sc.get("description"), f"{sc['id']} 缺少 description"
            assert sc["expected"].get("shape"), f"{sc['id']} 缺少轨迹形状期望"

    def test_wave4_specs_have_runners_and_judges(self):
        spec_ids = {sc["id"] for sc in SCENARIOS}
        runner_ids = set(SCENARIO_RUNNERS)
        assert spec_ids == runner_ids, (
            f"规格与执行器不一致：仅规格 {spec_ids - runner_ids}，仅执行器 {runner_ids - spec_ids}"
        )
        from scenarios import JUDGES

        assert set(JUDGES) == spec_ids, "每个场景都必须有判定函数"


class TestScenarioSpecSchema:
    def test_spec_has_no_unknown_top_level_keys(self):
        allowed = {
            "id", "name", "description", "expected",
            "must_not_execute", "evidence_level",
        }
        for sc in SCENARIOS:
            unknown = set(sc) - allowed
            assert not unknown, f"{sc['id']} 含未知规格字段：{sorted(unknown)}"

    def test_expected_has_no_unknown_keys(self):
        allowed = {"status", "error_code", "shape"}
        for sc in SCENARIOS:
            unknown = set(sc["expected"]) - allowed
            assert not unknown, f"{sc['id']}.expected 含未知字段：{sorted(unknown)}"

    def test_judge_functions_exist_for_all_scenarios(self):
        from scenarios import JUDGES

        for sc in SCENARIOS:
            assert sc["id"] in JUDGES, f"{sc['id']} 缺少判定函数"
        assert set(JUDGES) == {sc["id"] for sc in SCENARIOS}


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
