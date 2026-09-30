"""工具契约与调用链测试（WP-01）。

覆盖手册 3.5：
- 工具定义字段齐全（name/version/description/input_schema/output_schema/
  risk_level/timeout_ms/idempotent/allowed_roles）
- 调用链顺序：参数 Schema 校验 → 角色与策略守卫 → 幂等检查 → 超时控制
  → 执行 → 输出 Schema 校验 → 审计和轨迹
- 风险级别冻结 4 值
- 确定性哈希（幂等键与轨迹摘要的基础）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.agents import (  # noqa: E402
    ErrorCode,
    FakeClock,
    RiskLevel,
    RuntimeConfig,
    RulePolicyGuard,
    TaskConflictError,
    ToolContext,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    canonical_json,
    sha256_hex,
)

FROZEN_TOOL_FIELDS = [
    "name",
    "version",
    "description",
    "input_schema",
    "output_schema",
    "risk_level",
    "timeout_ms",
    "idempotent",
    "allowed_roles",
]
FROZEN_RISK_LEVELS = ["read_only", "write", "device_command", "sensitive"]


def make_tool(**overrides):
    base = dict(
        name="demo.tool",
        version="1.0.0",
        description="测试工具",
        input_schema={"type": "object", "required": [], "properties": {}},
        output_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
        risk_level=RiskLevel.READ_ONLY,
        timeout_ms=5000,
        idempotent=False,
        allowed_roles=("operator",),
        handler=lambda ctx: {"ok": True},
    )
    base.update(overrides)
    return ToolDefinition(**base)


def make_executor(registry=None, tool=None, clock=None, audit=None):
    registry = registry or ToolRegistry()
    if tool is not None:
        registry.register(tool)
    guard = RulePolicyGuard(RuntimeConfig())
    return registry, ToolExecutor(registry, guard, clock or FakeClock(), audit=audit)


class TestToolDefinitionContract:
    def test_definition_fields_frozen(self):
        tool = make_tool()
        for field_name in FROZEN_TOOL_FIELDS:
            assert hasattr(tool, field_name), f"工具定义缺少冻结字段 {field_name}"
        assert tool.name == "demo.tool"
        assert tool.risk_level == RiskLevel.READ_ONLY

    def test_definition_immutable(self):
        tool = make_tool()
        with pytest.raises(Exception):
            tool.name = "changed"  # frozen dataclass 不可变

    def test_risk_level_values_frozen(self):
        assert [r.value for r in RiskLevel] == FROZEN_RISK_LEVELS


class TestToolRegistry:
    def test_register_get_list(self):
        registry = ToolRegistry()
        registry.register(make_tool())
        assert registry.get("demo.tool") is not None
        assert registry.get("missing") is None
        assert [t.name for t in registry.list()] == ["demo.tool"]

    def test_duplicate_register_conflict(self):
        registry = ToolRegistry()
        registry.register(make_tool())
        with pytest.raises(TaskConflictError):
            registry.register(make_tool())


class TestCallChainOrder:
    """手册 3.5 调用链：参数校验 → 角色守卫 → 幂等 → 超时 → 执行 →
    输出校验 → 审计。每道闸门都必须挡住后续环节。"""

    def test_input_schema_error_blocks_execution(self):
        called = []
        tool = make_tool(
            input_schema={
                "type": "object",
                "required": ["event_id"],
                "properties": {"event_id": {"type": "string"}},
            },
            handler=lambda ctx: (called.append(1) or {"ok": True}),
        )
        _, executor = make_executor(tool=tool)
        result = executor.execute_once("run_1", "operator", "demo.tool", {})
        assert result.ok is False
        assert result.error_code == "invalid_tool_input"
        assert called == []  # 参数校验失败 → handler 不执行

    def test_role_guard_blocks_execution(self):
        called = []
        tool = make_tool(
            allowed_roles=("dispatcher",),
            handler=lambda ctx: (called.append(1) or {"ok": True}),
        )
        _, executor = make_executor(tool=tool)
        result = executor.execute_once("run_1", "operator", "demo.tool", {})
        assert result.ok is False
        assert result.error_code == "policy_denied"
        assert called == []

    def test_idempotent_tool_replays_cached_result(self):
        calls = []
        tool = make_tool(
            idempotent=True,
            handler=lambda ctx: (calls.append(1) or {"ok": True}),
        )
        _, executor = make_executor(tool=tool)
        first = executor.execute_once("run_1", "operator", "demo.tool", {})
        second = executor.execute_once("run_2", "operator", "demo.tool", {})
        assert first.ok and second.ok
        assert len(calls) == 1  # 第二次命中幂等缓存，不执行
        assert second.replayed is True
        assert first.output_hash == second.output_hash

    def test_non_idempotent_tool_executes_every_time(self):
        calls = []
        tool = make_tool(handler=lambda ctx: (calls.append(1) or {"ok": True}))
        _, executor = make_executor(tool=tool)
        executor.execute_once("run_1", "operator", "demo.tool", {})
        executor.execute_once("run_2", "operator", "demo.tool", {})
        assert len(calls) == 2

    def test_timeout_control(self):
        clock = FakeClock()

        def slow_handler(ctx):
            ctx.clock.advance(6000)
            return {"ok": True}

        tool = make_tool(timeout_ms=5000, handler=slow_handler)
        _, executor = make_executor(tool=tool, clock=clock)
        result = executor.execute_once("run_1", "operator", "demo.tool", {})
        assert result.ok is False
        assert result.error_code == "tool_timeout"
        assert result.latency_ms >= 6000

    def test_output_schema_error(self):
        tool = make_tool(handler=lambda ctx: {"ok": "not_a_bool"})
        _, executor = make_executor(tool=tool)
        result = executor.execute_once("run_1", "operator", "demo.tool", {})
        assert result.ok is False
        assert result.error_code == "invalid_tool_output"

    def test_handler_exception_becomes_tool_failed(self):
        tool = make_tool(handler=lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
        _, executor = make_executor(tool=tool)
        result = executor.execute_once("run_1", "operator", "demo.tool", {})
        assert result.ok is False
        assert result.error_code == "tool_failed"
        assert "boom" in result.error_message

    def test_audit_callback_receives_call_info(self):
        audited = []
        tool = make_tool()
        _, executor = make_executor(tool=tool, audit=audited.append)
        result = executor.execute_once("run_9", "operator", "demo.tool", {})
        assert result.ok
        assert len(audited) == 1
        info = audited[0]
        assert info["run_id"] == "run_9"
        assert info["tool_name"] == "demo.tool"
        assert info["tool_version"] == "1.0.0"
        assert info["role"] == "operator"
        assert info["input_hash"] and info["output_hash"]
        assert info["ok"] is True

    def test_missing_tool(self):
        _, executor = make_executor()
        result = executor.execute_once("run_1", "operator", "ghost.tool", {})
        assert result.ok is False
        assert result.error_code == "tool_failed"


class TestToolContext:
    def test_context_exposes_run_and_input(self):
        captured = {}

        def handler(ctx: ToolContext):
            captured["run_id"] = ctx.run_id
            captured["tool_name"] = ctx.tool_name
            captured["role"] = ctx.role
            captured["input"] = ctx.input
            return {"ok": True}

        tool = make_tool(
            input_schema={
                "type": "object",
                "required": ["x"],
                "properties": {"x": {"type": "integer"}},
            },
            handler=handler,
        )
        _, executor = make_executor(tool=tool)
        executor.execute_once("run_x", "operator", "demo.tool", {"x": 1})
        assert captured == {
            "run_id": "run_x",
            "tool_name": "demo.tool",
            "role": "operator",
            "input": {"x": 1},
        }


class TestDeterministicHashing:
    def test_canonical_json_is_deterministic(self):
        a = {"b": 2, "a": [1, {"y": True}], "c": "文本"}
        b = {"c": "文本", "a": [1, {"y": True}], "b": 2}
        assert canonical_json(a) == canonical_json(b)
        assert sha256_hex(a) == sha256_hex(b)

    def test_hash_differs_on_value_change(self):
        assert sha256_hex({"event_id": "a"}) != sha256_hex({"event_id": "b"})
