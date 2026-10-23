"""WP-12 轨迹回放模块契约测试（replay.py）。

验证（docs/agent-program-wave3.md 3.3 + 本工作包消息）：
- 比对维度固定：步骤类型 / 工具 / 错误码 / 状态 / 摘要哈希 / 输入输出哈希 / 终态。
- 不比较原始思维链：未知键（chain_of_thought / reasoning）一律忽略。
- trace_replay_match_rate：分子=匹配步骤数，分母=总步骤数；分母为零 → null。
- 自身回放 100% 匹配；篡改工具名 / 错误码 / 终态 / 摘要必须被检出。
- hash_trace 确定性（键排序，与输入键序无关）。

纯逻辑测试：不实例化 Runtime。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from replay import (  # noqa: E402
    compare_traces,
    hash_trace,
    sha256_summary,
    step_fingerprint,
    summarize_trace,
)


def _trace(steps=None, terminal="succeeded"):
    return {
        "terminal_status": terminal,
        "steps": steps
        if steps is not None
        else [
            {"step_type": "plan", "tool_name": None, "error_code": None, "status": "ok", "decision_summary": "计划"},
            {"step_type": "tool_call", "tool_name": "event.get", "error_code": None, "status": "ok", "decision_summary": "读取事件"},
            {"step_type": "tool_call", "tool_name": "mission.observe", "error_code": "tool_failed", "status": "failed", "decision_summary": "观察失败"},
            {"step_type": "terminal", "tool_name": None, "error_code": None, "status": "succeeded", "decision_summary": "完成"},
        ],
    }


class TestFingerprint:
    def test_step_fingerprint_whitelist(self):
        fp = step_fingerprint({
            "step_type": "tool_call",
            "tool_name": "event.get",
            "error_code": None,
            "status": "ok",
            "decision_summary": "读取事件",
            "chain_of_thought": "私有思维链",  # 未知键必须被忽略
            "reasoning": "内部推理",
        })
        assert set(fp) == {
            "step_type", "tool_name", "error_code", "status",
            "summary_hash", "input_hash", "output_hash",
        }
        assert "chain_of_thought" not in fp
        assert fp["tool_name"] == "event.get"

    def test_fingerprint_ignores_unknown_keys(self):
        a = _trace()["steps"][1]
        b = dict(a, extra_secret="x", raw_output="y")
        assert step_fingerprint(a) == step_fingerprint(b), "未知键不得参与比对（解密预算）"

    def test_fingerprint_accepts_objects(self):
        class _Step:
            step_type = "plan"
            tool_name = None
            error_code = None
            status = "ok"
            decision_summary = "对象摘要"
            input_hash = None
            output_hash = None

        fp = step_fingerprint(_Step())
        assert fp["step_type"] == "plan"
        assert fp["summary_hash"] == sha256_summary("对象摘要")

    def test_summary_hash_deterministic(self):
        assert sha256_summary("abc") == sha256_summary("abc")
        assert sha256_summary("abc") != sha256_summary("abd")
        assert sha256_summary(None) == sha256_summary("")


class TestCompareTraces:
    def test_identical_traces_match_perfectly(self):
        r = compare_traces(_trace(), _trace())
        assert r.match_rate == 1.0
        assert r.matched_steps == r.total_steps == 4
        assert r.terminal_match is True
        assert r.trace_hash_matches is True
        assert r.mismatches == []

    def test_empty_traces_null_rate(self):
        r = compare_traces({"terminal_status": None, "steps": []}, {"terminal_status": None, "steps": []})
        assert r.match_rate is None
        assert r.total_steps == 0
        assert r.terminal_match is True

    def test_tool_name_mismatch_detected(self):
        tampered = _trace()
        tampered["steps"][1]["tool_name"] = "event.tampered"
        r = compare_traces(_trace(), tampered)
        assert r.match_rate == 0.75
        assert r.matched_steps == 3
        assert any(m["index"] == 1 for m in r.mismatches)

    def test_error_code_mismatch_detected(self):
        tampered = _trace()
        tampered["steps"][2]["error_code"] = "tool_timeout"
        r = compare_traces(_trace(), tampered)
        assert r.match_rate == 0.75
        assert any(m["index"] == 2 for m in r.mismatches)

    def test_terminal_status_mismatch_detected(self):
        tampered = _trace(terminal="failed")
        r = compare_traces(_trace(), tampered)
        assert r.terminal_match is False
        assert r.trace_hash_matches is False

    def test_summary_change_detected_via_hash(self):
        """摘要哈希参与比对：decision_summary 变化必须被检出（不比较原始思维链 ≠ 不比较摘要）。"""
        tampered = _trace()
        tampered["steps"][0]["decision_summary"] = "另一版摘要"
        r = compare_traces(_trace(), tampered)
        assert r.match_rate == 0.75

    def test_cot_fields_do_not_affect_match(self):
        """思维链字段不参与比对：原始含思维链、回放不含 → 仍 100% 匹配。"""
        recorded = _trace()
        for s in recorded["steps"]:
            s["chain_of_thought"] = "模型私有推理"
            s["reasoning"] = "内部推理"
        r = compare_traces(recorded, _trace())
        assert r.match_rate == 1.0

    def test_missing_steps_detected(self):
        replayed = _trace()
        replayed["steps"] = replayed["steps"][:2]
        r = compare_traces(_trace(), replayed)
        assert r.total_steps == 4
        assert r.matched_steps == 2
        assert r.match_rate == 0.5

    def test_extra_steps_detected(self):
        replayed = _trace()
        replayed["steps"] = replayed["steps"] + [{"step_type": "extra"}]
        r = compare_traces(_trace(), replayed)
        assert r.total_steps == 5
        assert r.matched_steps == 4
        assert any(m["reason"].startswith("多余") for m in r.mismatches)


class TestTraceHashing:
    def test_hash_stable_across_key_order(self):
        t1 = _trace()
        t2 = {"steps": t1["steps"], "terminal_status": t1["terminal_status"]}
        assert hash_trace(t1) == hash_trace(t2)

    def test_hash_changes_on_any_change(self):
        t1 = _trace()
        t2 = copy.deepcopy(t1)
        t2["steps"][1]["tool_name"] = "other"
        assert hash_trace(t1) != hash_trace(t2)

    def test_hash_is_hex64(self):
        h = hash_trace(_trace())
        assert isinstance(h, str) and len(h) == 64
        assert set(h) <= set("0123456789abcdef")

    def test_summarize_trace_shape(self):
        summary = summarize_trace(_trace())
        assert summary["terminal_status"] == "succeeded"
        assert len(summary["steps"]) == 4
        assert "trace_hash" in summary
        assert summary["trace_hash"] == hash_trace(_trace())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
