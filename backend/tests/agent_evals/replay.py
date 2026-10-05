"""WP-12 轨迹回放：确定性比对原始轨迹与回放轨迹（docs/agent-program-wave3.md 3.3）。

定位：评测的「可复现性」证据 —— 同一 run 的轨迹在任何时间回放都必须与
原始记录一致。比对维度固定为：

    · 步骤类型（step_type）
    · 工具（tool_name）
    · 错误码（error_code）
    · 步骤状态（status）
    · 摘要哈希（decision_summary 的 sha256 —— 决策摘要参与比对）
    · 输入/输出哈希（input_hash / output_hash）
    · 终态（terminal_status）

★ 解密预算：**不比较原始思维链**。输入只接受已脱敏的摘要字段
（decision_summary / input_hash / output_hash / 工具名 / 错误码），
任何未知键（如 chain_of_thought / reasoning）一律忽略，绝不进入比对。

`trace_replay_match_rate` 口径：分子 = 匹配步骤数，分母 = 总步骤数
（两条轨迹步骤数不同时按较长的算，缺失/多余步骤计为不匹配）；
分母为零时输出 None（JSON 输出 null）。

证据等级：E1/E2 —— 只证明确定性环境下的轨迹可复现，不代表真实设备或现场。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# 参与比对的摘要字段（白名单）—— 未知键（含思维链）一律忽略
_FINGERPRINT_KEYS: tuple[str, ...] = (
    "step_type",
    "tool_name",
    "error_code",
    "status",
    "summary_hash",
    "input_hash",
    "output_hash",
)


def sha256_summary(text: Any) -> str:
    """对摘要文本求 sha256（空值 → 空串哈希，保持确定性）。"""
    if text is None:
        text = ""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _as_dict(step: Any) -> dict[str, Any]:
    """把步骤转成 dict：兼容 dict 与带属性的对象（如 AgentStep）。"""
    if isinstance(step, dict):
        return step
    out: dict[str, Any] = {}
    for key in (
        "step_type", "tool_name", "error_code", "status",
        "decision_summary", "input_hash", "output_hash",
    ):
        if hasattr(step, key):
            out[key] = getattr(step, key)
    return out


def step_fingerprint(step: Any) -> dict[str, str | None]:
    """单步规范指纹：只含比对白名单字段（摘要哈希化，不保留原始摘要全文）。"""
    raw = _as_dict(step)
    return {
        "step_type": raw.get("step_type"),
        "tool_name": raw.get("tool_name"),
        "error_code": raw.get("error_code"),
        "status": raw.get("status"),
        "summary_hash": sha256_summary(raw.get("decision_summary")),
        "input_hash": raw.get("input_hash"),
        "output_hash": raw.get("output_hash"),
    }


def summarize_trace(trace: Any) -> dict[str, Any]:
    """轨迹规范摘要：终态 + 逐步骤指纹 + 整体摘要哈希（可 JSON 序列化）。"""
    if isinstance(trace, dict):
        terminal = trace.get("terminal_status") or trace.get("terminal")
        steps = trace.get("steps") or []
    else:
        terminal = getattr(trace, "terminal_status", None) or getattr(trace, "terminal", None)
        steps = getattr(trace, "steps", None) or []
    fingerprints = [step_fingerprint(s) for s in steps]
    return {
        "terminal_status": terminal,
        "steps": fingerprints,
        "trace_hash": hash_trace({"terminal_status": terminal, "steps": steps}),
    }


def hash_trace(trace: Any) -> str:
    """整体轨迹摘要哈希：终态 + 全部步骤指纹（键排序，确定性）。"""
    payload = {
        "terminal_status": (
            trace.get("terminal_status") if isinstance(trace, dict) else getattr(trace, "terminal_status", None)
        ),
        "steps": [step_fingerprint(s) for s in (
            (trace.get("steps") or []) if isinstance(trace, dict) else (getattr(trace, "steps", None) or [])
        )],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class ReplayResult:
    """一次轨迹比对的结果（trace_replay_match_rate 的数据来源）。"""

    matched_steps: int
    total_steps: int
    match_rate: float | None
    terminal_match: bool
    trace_hash_matches: bool
    mismatches: list[dict[str, Any]] = field(default_factory=list)


def compare_traces(recorded: Any, replayed: Any) -> ReplayResult:
    """比对原始轨迹与回放轨迹，返回结构化结果。

    规则：
    - 分母 = max(len(recorded.steps), len(replayed.steps))；分子 = 逐位
      匹配的步骤数；分母为零 → match_rate = None（null 语义）。
    - 终态（terminal_status）不一致 → terminal_match = False。
    - 整体摘要哈希（hash_trace）不一致 → trace_hash_matches = False。
    - 每个不匹配位置记录 (index, reason)，reason ∈ 缺失/多余/不一致。
    """
    r_steps = ((recorded.get("steps") or []) if isinstance(recorded, dict) else (getattr(recorded, "steps", None) or []))
    p_steps = ((replayed.get("steps") or []) if isinstance(replayed, dict) else (getattr(replayed, "steps", None) or []))
    r_fp = [step_fingerprint(s) for s in r_steps]
    p_fp = [step_fingerprint(s) for s in p_steps]

    total = max(len(r_fp), len(p_fp))
    matched = 0
    mismatches: list[dict[str, Any]] = []
    for i in range(total):
        has_r = i < len(r_fp)
        has_p = i < len(p_fp)
        if not has_r:
            mismatches.append({"index": i, "reason": "多余步骤（回放比原始多）"})
            continue
        if not has_p:
            mismatches.append({"index": i, "reason": "缺失步骤（回放比原始少）"})
            continue
        if r_fp[i] == p_fp[i]:
            matched += 1
        else:
            mismatches.append({"index": i, "reason": "步骤指纹不一致"})

    r_terminal = (
        recorded.get("terminal_status") if isinstance(recorded, dict) else getattr(recorded, "terminal_status", None)
    )
    p_terminal = (
        replayed.get("terminal_status") if isinstance(replayed, dict) else getattr(replayed, "terminal_status", None)
    )
    terminal_match = r_terminal == p_terminal
    trace_hash_matches = hash_trace(recorded) == hash_trace(replayed)

    rate = (matched / total) if total > 0 else None
    return ReplayResult(
        matched_steps=matched,
        total_steps=total,
        match_rate=rate,
        terminal_match=terminal_match,
        trace_hash_matches=trace_hash_matches,
        mismatches=mismatches,
    )


__all__ = [
    "ReplayResult",
    "compare_traces",
    "hash_trace",
    "sha256_summary",
    "step_fingerprint",
    "summarize_trace",
]
