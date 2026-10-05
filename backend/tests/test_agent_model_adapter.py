"""WP-11 模型适配层与规则兜底测试（backend/tests/test_agent_model_adapter.py）。

覆盖 docs/agent-program-wave3.md 第 5 节的 8 个必测场景：
    1. 合法模型计划通过严格校验。
    2. 非法 JSON 回退规则模式。
    3. 缺字段或类型错误回退。
    4. 未知工具或越权角色回退。
    5. 模型超时回退。
    6. 模型返回敏感工具但角色无权限，回退且不执行。
    7. 配置关闭时完全不调用模型客户端。
    8. 日志和结果不含思维链字段（chain_of_thought / reasoning）。

另附加固场景：连接失败 / HTTP 错误 / 意外异常回退、参数白名单、
工具 Schema 违规、超长字段、步数上限、自定义规则计划兜底、
payload 脱敏、响应哈希、延迟测量、冻结接口契约守卫。

全部使用假客户端 + 假时钟 + 内存 Runtime，绝不访问公网。
"""

from __future__ import annotations

import json
import logging
import socket
import sys
from collections import Counter
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path
from urllib.error import HTTPError

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.agents import (  # noqa: E402
    AgentRunRequest,
    AgentRuntime,
    FakeClock,
    RiskLevel,
    RuntimeConfig,
    SequenceIdFactory,
    ToolDefinition,
    ToolRegistry,
    sha256_hex,
)
from app.services.agents.model_adapter import (  # noqa: E402
    DEFAULT_MODEL_SCHEMA,
    FORBIDDEN_FIELD_NAMES,
    MODEL_CONNECTION_FAILED,
    MODEL_DISABLED,
    MODEL_DISALLOWED_ARGUMENT,
    MODEL_FORBIDDEN_FIELD,
    MODEL_HTTP_ERROR,
    MODEL_INTERNAL_ERROR,
    MODEL_INVALID_JSON,
    MODEL_NOT_CONFIGURED,
    MODEL_ROLE_DENIED,
    MODEL_SCHEMA_ERROR,
    MODEL_TIMEOUT,
    MODEL_UNKNOWN_TOOL,
    JsonModelClient,
    ModelAdapterError,
    ModelAdapterPlanner,
    ModelPlanProposal,
    ModelSchemaError,
    ModelStepProposal,
    ModelTimeoutError,
    ModelUnavailableError,
    OpenAICompatibleModelClient,
    SOURCE_MODEL,
    SOURCE_RULE_FALLBACK,
    summarize_tools,
)

# ----------------------------------------------------------------------
# 确定性测试工具集（假客户端 / 桩工具集 / 请求构造）
# ----------------------------------------------------------------------


class FakeJsonClient:
    """假模型客户端：返回固定结果或抛固定异常，记录调用次数与 payload。"""

    def __init__(self, result=None, *, error=None):
        self.result = result
        self.error = error
        self.calls = 0
        self.payloads: list[dict] = []

    def complete(self, payload):
        self.calls += 1
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return self.result


class FakeHTTPResponse:
    """OpenAI-compatible 传输单元测试使用的响应桩。"""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def make_registry():
    """构造桩工具集（含一个仅 admin 可用的敏感工具 mqtt.send_task）。"""
    registry = ToolRegistry()
    calls: Counter = Counter()

    def register(name, risk, handler, *, roles=("operator", "dispatcher"), input_schema=None, output_schema=None, idempotent=False):
        def wrapped(ctx, _name=name):
            calls[_name] += 1
            return handler(ctx)

        registry.register(
            ToolDefinition(
                name=name,
                version="1.0.0",
                description=f"桩工具 {name}",
                input_schema=input_schema or {"type": "object", "required": [], "properties": {}},
                output_schema=output_schema or {"type": "object", "required": [], "properties": {}},
                risk_level=risk,
                timeout_ms=5000,
                idempotent=idempotent,
                allowed_roles=roles,
                handler=wrapped,
            )
        )

    register(
        "event.get",
        RiskLevel.READ_ONLY,
        lambda ctx: {"event_id": ctx.input["event_id"], "confirmed": True},
        input_schema={"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "string"}}},
    )
    register(
        "device.query_available",
        RiskLevel.READ_ONLY,
        lambda ctx: {"candidates": [{"robot_id": "rb_01", "battery": 90}]},
        input_schema={"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["candidates"], "properties": {"candidates": {"type": "array", "items": {"type": "object"}}}},
    )
    register(
        "dispatch.plan",
        RiskLevel.READ_ONLY,
        lambda ctx: {"robot_id": ctx.input["candidates"][0]["robot_id"], "score": 0.9},
        input_schema={"type": "object", "required": ["event_id", "candidates"], "properties": {"event_id": {"type": "string"}, "candidates": {"type": "array"}}},
        output_schema={"type": "object", "required": ["robot_id"], "properties": {"robot_id": {"type": "string"}}},
    )
    register(
        "task.create_or_merge",
        RiskLevel.WRITE,
        lambda ctx: {"task_id": "tsk_0001", "action": "created"},
        input_schema={"type": "object", "required": ["event_id", "robot_id"], "properties": {"event_id": {"type": "string"}, "robot_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}},
        idempotent=True,
    )
    register(
        "mission.observe",
        RiskLevel.READ_ONLY,
        lambda ctx: {"status": "done", "progress": 1.0},
        input_schema={"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}},
    )
    register(
        "mqtt.send_task",
        RiskLevel.SENSITIVE,
        lambda ctx: {"sent": True},
        roles=("admin",),
        input_schema={"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["sent"], "properties": {"sent": {"type": "boolean"}}},
    )
    return registry, calls


def make_request(**overrides):
    """构造确定性派单请求；role 默认 operator。"""
    params = dict(overrides.pop("params", None) or {"event_id": "evt_001"})
    defaults = dict(
        trigger_type="event",
        objective="处理海漂垃圾事件并完成派单闭环",
        actor="operator-01",
        role="operator",
        params=params,
    )
    defaults.update(overrides)
    return AgentRunRequest(**defaults)


def make_adapter(client, *, enabled=True, **overrides):
    """构造适配器：默认注入假客户端 + 假时钟 + 确定性配置。"""
    return ModelAdapterPlanner(client=client, enabled=enabled, clock=FakeClock(), **overrides)


def valid_plan_response():
    """合法模型响应：2 步，全部使用已注册工具与白名单参数。"""
    return {
        "plan": [
            {
                "tool_name": "event.get",
                "arguments": {"event_id": "evt_001"},
                "decision_summary": "读取事件与证据",
                "confidence": 0.95,
            },
            {
                "tool_name": "device.query_available",
                "arguments": {"event_id": "evt_001"},
                "decision_summary": "查询可用机器人",
                "confidence": 0.9,
            },
        ]
    }


def rule_fallback_tool_names():
    """默认规则计划（DEFAULT_RULE_PLAN）的工具序列。"""
    return [
        "event.get",
        "device.query_available",
        "dispatch.plan",
        "task.create_or_merge",
        "mission.observe",
    ]


def assert_no_forbidden_fields(value) -> None:
    """递归断言任何对象（结果 / 日志文本 / payload）不含思维链字段。"""
    text = str(value)
    for name in FORBIDDEN_FIELD_NAMES:
        assert name not in text.lower(), f"出现被禁止字段名 {name!r}"


def walk_keys(value, _keys=None):
    """收集 dict 的全部键名（递归）。"""
    keys = [] if _keys is None else _keys
    if isinstance(value, dict):
        for key, sub in value.items():
            keys.append(key)
            walk_keys(sub, keys)
    elif isinstance(value, (list, tuple)):
        for item in value:
            walk_keys(item, keys)
    return keys


# ======================================================================
# 场景 1：合法模型计划通过严格校验
# ======================================================================


class TestValidModelPlan:
    def test_valid_model_plan_accepted(self):
        registry, _ = make_registry()
        client = FakeJsonClient(result=valid_plan_response())
        adapter = make_adapter(client)

        proposal = adapter.plan(
            task=make_request(),
            business_context={"region": "A区", "confirmed": True},
            tool_summary=summarize_tools(registry),
            role="operator",
            schema=DEFAULT_MODEL_SCHEMA,
        )

        assert proposal.source == SOURCE_MODEL
        assert proposal.error_code is None
        assert proposal.fallback_reason is None
        assert len(proposal.steps) == 2
        assert len(proposal.model_steps) == 2
        assert proposal.response_hash == sha256_hex(client.result)
        assert proposal.latency_ms == 0
        assert client.calls == 1
        # 模型步骤只含 4 个结构化字段
        step = proposal.model_steps[0]
        assert isinstance(step, ModelStepProposal)
        assert step.tool_name == "event.get"
        assert step.arguments == {"event_id": "evt_001"}
        assert step.decision_summary == "读取事件与证据"
        assert step.confidence == 0.95
        # 平均置信度
        assert proposal.confidence == pytest.approx((0.95 + 0.9) / 2)
        # Runtime 可消费的 RuleStep
        from app.services.agents import RuleStep

        assert all(isinstance(s, RuleStep) for s in proposal.steps)
        assert proposal.steps[0].tool == "event.get"
        assert proposal.steps[0].input == {"event_id": "evt_001"}

    def test_model_plan_runs_through_runtime_closed_loop(self):
        """模型计划可直接被 AgentRuntime 消费，派单闭环仍工作。"""
        registry, calls = make_registry()
        client = FakeJsonClient(result=valid_plan_response())
        adapter = make_adapter(client)
        request = make_request()

        proposal = adapter.plan(
            task=request,
            business_context={},
            tool_summary=summarize_tools(registry),
            role=request.role,
        )
        assert proposal.source == SOURCE_MODEL

        runtime = AgentRuntime(
            tool_registry=registry,
            clock=FakeClock(),
            id_factory=SequenceIdFactory(),
            runtime_config=RuntimeConfig(
                rule_plan=[
                    {"tool": s.tool, "input": dict(s.input), "summary": s.summary}
                    for s in proposal.steps
                ]
            ),
        )
        result = runtime.run(request)
        assert result.status == "succeeded"
        assert calls["event.get"] == 1
        assert calls["device.query_available"] == 1
        # 模型未提议的敏感工具绝不执行
        assert calls["mqtt.send_task"] == 0


# ======================================================================
# 场景 2：非法 JSON 回退规则模式
# ======================================================================


class TestInvalidJsonFallback:
    def test_unparseable_string_falls_back(self):
        registry, _ = make_registry()
        client = FakeJsonClient(result='{not-json{{{')
        adapter = make_adapter(client)

        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")

        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_INVALID_JSON
        assert proposal.fallback_reason is not None
        assert proposal.fallback_reason.startswith(MODEL_INVALID_JSON)
        assert proposal.model_steps == ()
        assert [s.tool for s in proposal.steps] == rule_fallback_tool_names()
        assert client.calls == 1

    def test_empty_string_falls_back(self):
        registry, _ = make_registry()
        adapter = make_adapter(FakeJsonClient(result="   "))
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_INVALID_JSON

    def test_json_scalar_not_object_falls_back(self):
        registry, _ = make_registry()
        # "42" 是合法 JSON 但不是对象 → 结构错误回退
        adapter = make_adapter(FakeJsonClient(result="42"))
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_SCHEMA_ERROR

    def test_fallback_is_observable_and_not_faked(self):
        registry, _ = make_registry()
        adapter = make_adapter(FakeJsonClient(result="broken"))
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        # 明确可观测：来源、原因、错误码齐全；decision_summary 声明规则兜底
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_INVALID_JSON
        assert "规则兜底" in proposal.decision_summary


# ======================================================================
# 场景 3：缺字段或类型错误回退
# ======================================================================


class TestMissingFieldOrTypeError:
    def _fallback_of(self, response, role="operator"):
        registry, _ = make_registry()
        adapter = make_adapter(FakeJsonClient(result=response))
        proposal = adapter.plan(task=make_request(role=role), tool_summary=summarize_tools(registry), role=role)
        assert proposal.source == SOURCE_RULE_FALLBACK
        return proposal

    def test_step_missing_required_field(self):
        response = {
            "plan": [{"tool_name": "event.get", "arguments": {"event_id": "x"}, "confidence": 0.5}]
        }
        proposal = self._fallback_of(response)
        assert proposal.error_code == MODEL_SCHEMA_ERROR

    def test_arguments_wrong_type(self):
        response = {
            "plan": [
                {
                    "tool_name": "event.get",
                    "arguments": "not-a-dict",
                    "decision_summary": "s",
                    "confidence": 0.5,
                }
            ]
        }
        proposal = self._fallback_of(response)
        assert proposal.error_code == MODEL_SCHEMA_ERROR

    def test_top_level_missing_plan(self):
        proposal = self._fallback_of({"decision_summary": "空计划"})
        assert proposal.error_code == MODEL_SCHEMA_ERROR

    def test_top_level_unknown_key(self):
        response = {"plan": [{"tool_name": "event.get", "arguments": {"event_id": "x"}, "decision_summary": "s", "confidence": 0.5}], "extra": 1}
        proposal = self._fallback_of(response)
        assert proposal.error_code == MODEL_SCHEMA_ERROR

    def test_bare_array_rejected(self):
        proposal = self._fallback_of([{"tool_name": "event.get"}])
        assert proposal.error_code == MODEL_SCHEMA_ERROR

    def test_overlong_field_rejected(self):
        response = {
            "plan": [
                {
                    "tool_name": "event.get",
                    "arguments": {"event_id": "x"},
                    "decision_summary": "长" * 250,
                    "confidence": 0.5,
                }
            ]
        }
        proposal = self._fallback_of(response)
        assert proposal.error_code == MODEL_SCHEMA_ERROR

    def test_confidence_out_of_range(self):
        response = {
            "plan": [
                {
                    "tool_name": "event.get",
                    "arguments": {"event_id": "x"},
                    "decision_summary": "s",
                    "confidence": 1.5,
                }
            ]
        }
        proposal = self._fallback_of(response)
        assert proposal.error_code == MODEL_SCHEMA_ERROR


# ======================================================================
# 场景 4：未知工具或越权角色回退
# ======================================================================


class TestUnknownToolOrRoleDenied:
    def test_unknown_tool_falls_back(self):
        registry, _ = make_registry()
        response = {
            "plan": [
                {
                    "tool_name": "hack.delete_all",
                    "arguments": {},
                    "decision_summary": "危险操作",
                    "confidence": 0.99,
                }
            ]
        }
        adapter = make_adapter(FakeJsonClient(result=response))
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_UNKNOWN_TOOL
        assert [s.tool for s in proposal.steps] == rule_fallback_tool_names()

    def test_role_not_allowed_falls_back(self):
        registry, _ = make_registry()
        response = {
            "plan": [
                {
                    "tool_name": "event.get",
                    "arguments": {"event_id": "x"},
                    "decision_summary": "读事件",
                    "confidence": 0.8,
                }
            ]
        }
        adapter = make_adapter(FakeJsonClient(result=response))
        # viewer 不在任何工具的 allowed_roles 内
        proposal = adapter.plan(task=make_request(role="viewer"), tool_summary=summarize_tools(registry), role="viewer")
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_ROLE_DENIED


# ======================================================================
# 场景 5：模型超时回退
# ======================================================================


class TestTimeoutFallback:
    def _plan(self, error, role="operator"):
        registry, _ = make_registry()
        adapter = make_adapter(FakeJsonClient(error=error))
        return adapter.plan(task=make_request(role=role), tool_summary=summarize_tools(registry), role=role)

    def test_model_timeout_error_falls_back(self):
        proposal = self._plan(ModelTimeoutError("模型响应超时"))
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_TIMEOUT

    def test_builtin_timeout_falls_back(self):
        proposal = self._plan(TimeoutError("socket timeout"))
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_TIMEOUT

    def test_connection_failure_falls_back(self):
        proposal = self._plan(ConnectionError("connection refused"))
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_CONNECTION_FAILED

    def test_http_error_falls_back(self):
        proposal = self._plan(ModelAdapterError("HTTP 503", code=MODEL_HTTP_ERROR))
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_HTTP_ERROR

    def test_unexpected_exception_falls_back(self):
        proposal = self._plan(RuntimeError("奇怪异常"))
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_INTERNAL_ERROR

    def test_fallback_keeps_rule_plan(self):
        proposal = self._plan(ModelTimeoutError("超时"))
        assert [s.tool for s in proposal.steps] == rule_fallback_tool_names()


# ======================================================================
# 场景 6：模型返回敏感工具但角色无权限 → 回退且不执行
# ======================================================================


class TestSensitiveToolDenied:
    def test_sensitive_tool_without_role_denied_and_not_executed(self):
        registry, calls = make_registry()
        response = {
            "plan": [
                {
                    "tool_name": "mqtt.send_task",
                    "arguments": {"task_id": "tsk_x"},
                    "decision_summary": "下发设备指令",
                    "confidence": 0.9,
                }
            ]
        }
        adapter = make_adapter(FakeJsonClient(result=response))
        request = make_request(role="operator")  # operator 无权限（仅 admin）

        proposal = adapter.plan(
            task=request,
            tool_summary=summarize_tools(registry),
            role=request.role,
        )

        # 回退且结构可观测
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_ROLE_DENIED
        assert "敏感" in (proposal.fallback_reason or "")
        # 兜底计划绝不包含敏感工具
        assert "mqtt.send_task" not in [s.tool for s in proposal.steps]

        # 用兜底计划跑完整派单闭环：成功且敏感工具 0 次执行
        runtime = AgentRuntime(
            tool_registry=registry,
            clock=FakeClock(),
            id_factory=SequenceIdFactory(),
            runtime_config=RuntimeConfig(
                rule_plan=[
                    {"tool": s.tool, "input": dict(s.input), "summary": s.summary}
                    for s in proposal.steps
                ]
            ),
        )
        result = runtime.run(request)
        assert result.status == "succeeded"
        assert calls["mqtt.send_task"] == 0
        assert calls["event.get"] == 1
        assert calls["mission.observe"] == 1

    def test_admin_role_may_propose_sensitive_tool(self):
        registry, _ = make_registry()
        response = {
            "plan": [
                {
                    "tool_name": "mqtt.send_task",
                    "arguments": {"task_id": "tsk_x"},
                    "decision_summary": "下发设备指令",
                    "confidence": 0.9,
                }
            ]
        }
        adapter = make_adapter(FakeJsonClient(result=response))
        request = make_request(role="admin")
        proposal = adapter.plan(task=request, tool_summary=summarize_tools(registry), role="admin")
        assert proposal.source == SOURCE_MODEL
        assert proposal.model_steps[0].tool_name == "mqtt.send_task"


# ======================================================================
# 场景 7：配置关闭时完全不调用模型客户端（默认关闭）
# ======================================================================


class TestConfigDisabled:
    def test_default_disabled_never_calls_client(self):
        registry, _ = make_registry()
        client = FakeJsonClient(result=valid_plan_response())
        adapter = make_adapter(client, enabled=False)  # 默认关闭
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")

        assert client.calls == 0  # 关键：完全没调用
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_DISABLED
        assert [s.tool for s in proposal.steps] == rule_fallback_tool_names()

    def test_enabled_without_client_falls_back(self):
        registry, _ = make_registry()
        adapter = make_adapter(None, enabled=True)
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_NOT_CONFIGURED

    def test_plan_without_args_defaults_disabled(self):
        """不给任何参数时，默认关闭 + 无客户端 → 规则兜底。"""
        registry, _ = make_registry()
        adapter = ModelAdapterPlanner(clock=FakeClock())
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry))
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_DISABLED


# ======================================================================
# OpenAI-compatible 传输：请求映射与错误边界（全部使用内存 opener）
# ======================================================================
class TestOpenAICompatibleModelClient:
    def test_builds_chat_request_and_returns_json_content(self):
        captured: dict[str, object] = {}

        def opener(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeHTTPResponse(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(valid_plan_response())
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
            )

        client = OpenAICompatibleModelClient(
            base_url="https://model.example/v1",
            model="agent-model",
            api_key="sk-test",
            timeout_ms=2500,
            opener=opener,
        )
        result = client.complete({"task": {"objective": "dispatch"}})

        assert json.loads(result) == valid_plan_response()
        request = captured["request"]
        assert request.full_url == "https://model.example/v1/chat/completions"
        assert request.get_header("Authorization") == "Bearer sk-test"
        assert captured["timeout"] == 2.5
        envelope = json.loads(request.data.decode("utf-8"))
        assert envelope["model"] == "agent-model"
        assert envelope["messages"][0]["role"] == "system"
        assert "chain-of-thought" in envelope["messages"][0]["content"]
        assert envelope["response_format"] == {"type": "json_object"}
        assert "dispatch" in envelope["messages"][1]["content"]

    def test_accepts_full_chat_completions_url(self):
        def opener(request, *, timeout):
            del timeout
            assert request.full_url == "https://model.example/v1/chat/completions"
            return FakeHTTPResponse(
                b'{"choices":[{"message":{"content":"{\\"plan\\":[]}"}}]}'
            )

        client = OpenAICompatibleModelClient(
            base_url="https://model.example/v1/chat/completions",
            model="agent-model",
            opener=opener,
        )
        assert client.complete({}) == '{"plan":[]}'

    def test_timeout_maps_to_structured_error(self):
        def opener(request, *, timeout):
            del request, timeout
            raise socket.timeout("slow")

        client = OpenAICompatibleModelClient(
            base_url="https://model.example/v1",
            model="agent-model",
            opener=opener,
        )
        with pytest.raises(ModelTimeoutError) as exc_info:
            client.complete({})
        assert exc_info.value.code == MODEL_TIMEOUT

    def test_http_error_maps_to_transport_error(self):
        def opener(request, *, timeout):
            del timeout
            raise HTTPError(request.full_url, 503, "busy", {}, None)

        client = OpenAICompatibleModelClient(
            base_url="https://model.example/v1",
            model="agent-model",
            opener=opener,
        )
        with pytest.raises(ModelAdapterError) as exc_info:
            client.complete({})
        assert exc_info.value.code == MODEL_HTTP_ERROR

    def test_invalid_envelope_and_missing_content_fall_back_cleanly(self):
        bad_json = OpenAICompatibleModelClient(
            base_url="https://model.example/v1",
            model="agent-model",
            opener=lambda request, *, timeout: FakeHTTPResponse(b"not-json"),
        )
        with pytest.raises(ModelAdapterError) as bad_json_exc:
            bad_json.complete({})
        assert bad_json_exc.value.code == MODEL_INVALID_JSON

        missing = OpenAICompatibleModelClient(
            base_url="https://model.example/v1",
            model="agent-model",
            opener=lambda request, *, timeout: FakeHTTPResponse(
                b'{"choices":[{"message":{}}]}'
            ),
        )
        with pytest.raises(ModelSchemaError) as missing_exc:
            missing.complete({})
        assert missing_exc.value.code == MODEL_SCHEMA_ERROR


# ======================================================================
# 场景 8：日志和结果不含思维链字段（chain_of_thought / reasoning）
# ======================================================================


class TestNoChainOfThought:
    def test_forbidden_field_rejected_and_logs_clean(self, caplog):
        registry, _ = make_registry()
        response = {
            "plan": [
                {
                    "tool_name": "event.get",
                    "arguments": {"event_id": "x"},
                    "decision_summary": "s",
                    "confidence": 0.5,
                    "chain_of_thought": "秘密推理过程……",
                }
            ]
        }
        client = FakeJsonClient(result=response)
        adapter = make_adapter(client)
        with caplog.at_level(logging.INFO, logger="seasight.agents.model_adapter"):
            proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")

        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_FORBIDDEN_FIELD
        # 结果对象（含 fallback_reason / decision_summary / 全部字段）干净
        assert_no_forbidden_fields(proposal)
        assert_no_forbidden_fields(proposal.fallback_reason)
        # 日志不含思维链字段
        assert caplog.records, "必须产生日志记录"
        for record in caplog.records:
            assert_no_forbidden_fields(record.message)

    def test_reasoning_field_rejected(self):
        registry, _ = make_registry()
        response = {
            "plan": [
                {
                    "tool_name": "event.get",
                    "arguments": {"event_id": "x"},
                    "decision_summary": "s",
                    "confidence": 0.5,
                    "reasoning": "另一处推理",
                }
            ]
        }
        adapter = make_adapter(FakeJsonClient(result=response))
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_FORBIDDEN_FIELD

    def test_valid_model_result_and_logs_clean(self, caplog):
        registry, _ = make_registry()
        client = FakeJsonClient(result=valid_plan_response())
        adapter = make_adapter(client)
        with caplog.at_level(logging.INFO, logger="seasight.agents.model_adapter"):
            proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")

        assert proposal.source == SOURCE_MODEL
        assert_no_forbidden_fields(proposal)
        # 日志只含摘要信息，不含模型原始回复或完整 prompt
        for record in caplog.records:
            assert_no_forbidden_fields(record.message)
            assert "evt_001" not in record.message  # 不泄露业务参数
            assert "秘密" not in record.message


# ======================================================================
# 加固场景：payload 脱敏
# ======================================================================


class TestSanitizedPayload:
    def test_payload_drops_sensitive_and_cot_fields(self):
        registry, _ = make_registry()
        business_context = {
            "region": "A区",
            "api_key": "sk-very-secret",
            "authorization": "Bearer xxx",
            "chain_of_thought": "不应外泄",
            "nested": {"password": "pwd", "ok": 1},
            "raw_event": "x" * 500,  # 超长字段
        }
        client = FakeJsonClient(result=valid_plan_response())
        adapter = make_adapter(client)
        adapter.plan(
            task=make_request(params={"event_id": "evt_001"}),
            business_context=business_context,
            tool_summary=summarize_tools(registry),
            role="operator",
        )

        payload = client.payloads[0]
        # 递归断言：不含任何敏感键 / 思维链键
        keys = walk_keys(payload)
        for banned in ("api_key", "authorization", "chain_of_thought", "password", "token", "secret"):
            assert banned not in keys, f"payload 泄露敏感键 {banned}"
        # 超长字符串被截断
        raw_event = payload["business_context"]["raw_event"]
        assert len(raw_event) <= 203
        # 工具摘要只有契约字段，无 handler
        for tool in payload["tools"]:
            assert "handler" not in tool
            assert tool["name"]
            assert "risk_level" in tool
            assert "allowed_roles" in tool
            assert "input_schema" in tool
        # 任务信息只含脱敏摘要
        task = payload["task"]
        assert task["objective"] == "处理海漂垃圾事件并完成派单闭环"
        assert task["param_keys"] == ["event_id"]
        assert task["params"] == {"event_id": "evt_001"}
        # 严格 Schema 原样下传
        assert payload["output_schema"] == DEFAULT_MODEL_SCHEMA
        assert payload["hints"]["timeout_ms"] == 5000


# ======================================================================
# 加固场景：参数白名单 / 工具 Schema / 自定义规则计划 / 上限 / 延迟
# ======================================================================


class TestArgumentWhitelist:
    def _fallback_of(self, arguments):
        registry, _ = make_registry()
        response = {
            "plan": [
                {
                    "tool_name": "event.get",
                    "arguments": arguments,
                    "decision_summary": "s",
                    "confidence": 0.5,
                }
            ]
        }
        adapter = make_adapter(FakeJsonClient(result=response))
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert proposal.source == SOURCE_RULE_FALLBACK
        return proposal

    def test_unknown_argument_rejected(self):
        proposal = self._fallback_of({"event_id": "x", "hack": 1})
        assert proposal.error_code == MODEL_DISALLOWED_ARGUMENT

    def test_argument_violates_tool_schema(self):
        # event_id 必须是字符串
        proposal = self._fallback_of({"event_id": 123})
        assert proposal.error_code == MODEL_DISALLOWED_ARGUMENT


class TestMaxStepsCap:
    def test_plan_exceeding_cap_falls_back(self):
        registry, _ = make_registry()
        client = FakeJsonClient(result=valid_plan_response())
        adapter = make_adapter(client, max_steps=1)
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert proposal.error_code == MODEL_SCHEMA_ERROR


class TestCustomRulePlanFallback:
    def test_fallback_respects_custom_rule_plan(self):
        registry, _ = make_registry()
        config = RuntimeConfig(
            rule_plan=[{"tool": "event.get", "input": {"event_id": "{event_id}"}, "summary": "只读事件"}]
        )
        adapter = make_adapter(
            FakeJsonClient(error=ModelTimeoutError("超时")),
            config=config,
        )
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert proposal.source == SOURCE_RULE_FALLBACK
        assert [s.tool for s in proposal.steps] == ["event.get"]

    def test_broken_rule_plan_raises_model_adapter_error(self):
        registry, _ = make_registry()
        config = RuntimeConfig(rule_plan=[{"no_tool": 1}])
        adapter = make_adapter(
            FakeJsonClient(error=ModelTimeoutError("超时")),
            config=config,
        )
        with pytest.raises(ModelAdapterError) as exc_info:
            adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert exc_info.value.code == MODEL_INTERNAL_ERROR


class TestLatencyAndHash:
    def test_latency_recorded_from_client_call(self):
        registry, _ = make_registry()
        clock = FakeClock()

        class SlowClient(FakeJsonClient):
            def complete(self, payload):
                clock.advance(250)
                return super().complete(payload)

        client = SlowClient(result=valid_plan_response())
        adapter = ModelAdapterPlanner(client=client, enabled=True, clock=clock)
        proposal = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert proposal.source == SOURCE_MODEL
        assert proposal.latency_ms == 250

    def test_response_hash_stable_and_recorded(self):
        registry, _ = make_registry()
        client = FakeJsonClient(result=valid_plan_response())
        adapter = make_adapter(client)
        first = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        second = adapter.plan(task=make_request(), tool_summary=summarize_tools(registry), role="operator")
        assert first.response_hash == second.response_hash == sha256_hex(valid_plan_response())


# ======================================================================
# Runtime 接线：模型优先、规则兜底、状态与重规划可观测
# ======================================================================


class TestRuntimeAdapterIntegration:
    def test_runtime_consumes_model_plan_and_reports_source(self):
        registry, calls = make_registry()
        client = FakeJsonClient(result=valid_plan_response())
        config = RuntimeConfig()
        adapter = make_adapter(client, config=config)
        runtime = AgentRuntime(
            tool_registry=registry,
            clock=FakeClock(),
            id_factory=SequenceIdFactory(),
            runtime_config=config,
            model_adapter=adapter,
        )

        result = runtime.run(make_request())

        assert result.status == "succeeded"
        assert calls["event.get"] == 1
        assert calls["device.query_available"] == 1
        assert client.calls == 1
        plan_step = next(s for s in result.steps if s.step_type == "plan")
        assert "source=model" in plan_step.decision_summary
        assert runtime.status().model_available is True
        assert runtime.status().rule_mode is False

    def test_runtime_uses_rule_fallback_when_model_output_invalid(self):
        registry, calls = make_registry()
        config = RuntimeConfig(
            rule_plan=[
                {
                    "tool": "event.get",
                    "input": {"event_id": "{event_id}"},
                    "summary": "只读事件",
                }
            ]
        )
        client = FakeJsonClient(result="{not-json")
        adapter = make_adapter(client, config=config)
        runtime = AgentRuntime(
            tool_registry=registry,
            clock=FakeClock(),
            id_factory=SequenceIdFactory(),
            runtime_config=config,
            model_adapter=adapter,
        )

        result = runtime.run(make_request())

        assert result.status == "succeeded"
        assert calls["event.get"] == 1
        assert client.calls == 1
        plan_step = next(s for s in result.steps if s.step_type == "plan")
        assert "source=rule_fallback" in plan_step.decision_summary
        assert f"code={MODEL_INVALID_JSON}" in plan_step.decision_summary

    def test_runtime_replan_uses_adapter_again_and_records_fallback(self):
        registry = ToolRegistry()
        calls = Counter()

        def observe(ctx):
            calls["mission.observe"] += 1
            status = "waiting" if calls["mission.observe"] == 1 else "done"
            return {"status": status}

        registry.register(
            ToolDefinition(
                name="mission.observe",
                version="1.0.0",
                description="观察任务",
                input_schema={
                    "type": "object",
                    "required": ["task_id"],
                    "properties": {"task_id": {"type": "string"}},
                },
                output_schema={
                    "type": "object",
                    "required": ["status"],
                    "properties": {"status": {"type": "string"}},
                },
                risk_level=RiskLevel.READ_ONLY,
                timeout_ms=5000,
                idempotent=False,
                allowed_roles=("operator",),
                handler=observe,
            )
        )
        config = RuntimeConfig(
            rule_plan=[
                {
                    "tool": "mission.observe",
                    "input": {"task_id": "{task_id}"},
                    "summary": "观察任务",
                    "expect": {
                        "key": "status",
                        "contains": ("done",),
                        "on_violation": "replan",
                        "error_code": "tool_failed",
                    },
                }
            ],
            max_replans=2,
        )
        client = FakeJsonClient(result="broken")
        adapter = make_adapter(client, config=config)
        runtime = AgentRuntime(
            tool_registry=registry,
            clock=FakeClock(),
            id_factory=SequenceIdFactory(),
            runtime_config=config,
            model_adapter=adapter,
        )

        result = runtime.run(make_request(params={"task_id": "tsk_001"}))

        assert result.status == "succeeded"
        assert calls["mission.observe"] == 2
        assert client.calls == 2
        plan_steps = [s for s in result.steps if s.step_type == "plan"]
        assert len(plan_steps) == 2
        for step in plan_steps:
            assert "source=rule_fallback" in step.decision_summary
            assert f"code={MODEL_INVALID_JSON}" in step.decision_summary
        assert any(s.step_type == "replan" for s in result.steps)

    def test_injected_disabled_adapter_never_calls_client(self):
        registry, _ = make_registry()
        config = RuntimeConfig(
            rule_plan=[
                {"tool": "event.get", "input": {"event_id": "{event_id}"}, "summary": "只读事件"}
            ]
        )
        client = FakeJsonClient(result=valid_plan_response())
        adapter = make_adapter(client, enabled=False, config=config)
        runtime = AgentRuntime(
            tool_registry=registry,
            clock=FakeClock(),
            id_factory=SequenceIdFactory(),
            runtime_config=config,
            model_adapter=adapter,
        )

        result = runtime.run(make_request())

        assert result.status == "succeeded"
        assert client.calls == 0
        assert runtime.status().model_available is False
        assert runtime.status().rule_mode is True
        plan_step = next(s for s in result.steps if s.step_type == "plan")
        assert "source=rule_fallback" in plan_step.decision_summary
        assert f"code={MODEL_DISABLED}" in plan_step.decision_summary


# ======================================================================
# 冻结接口契约守卫（docs/agent-program-wave3.md 3.2）
# ======================================================================


class TestFrozenContract:
    FROZEN_NAMES = [
        "JsonModelClient",
        "ModelStepProposal",
        "ModelPlanProposal",
        "ModelAdapterPlanner",
        "ModelAdapterError",
        "ModelUnavailableError",
        "ModelTimeoutError",
        "ModelSchemaError",
    ]

    def test_public_names_exist(self):
        import app.services.agents.model_adapter as module

        for name in self.FROZEN_NAMES:
            assert hasattr(module, name), f"缺少冻结公开类 {name}"

    def test_error_hierarchy_and_codes(self):
        assert issubclass(ModelUnavailableError, ModelAdapterError)
        assert issubclass(ModelTimeoutError, ModelAdapterError)
        assert issubclass(ModelSchemaError, ModelAdapterError)
        assert ModelAdapterError("x").code == MODEL_INTERNAL_ERROR
        assert ModelTimeoutError("x").code == MODEL_TIMEOUT
        assert ModelUnavailableError("x").code == MODEL_CONNECTION_FAILED
        assert ModelSchemaError("x").code == MODEL_SCHEMA_ERROR
        assert ModelSchemaError("x", code=MODEL_UNKNOWN_TOOL).code == MODEL_UNKNOWN_TOOL

    def test_dataclasses_are_frozen(self):
        for cls in (ModelStepProposal, ModelPlanProposal):
            assert is_dataclass(cls)
            obj = cls.__new__(cls)  # 只验证冻结属性，不构造完整对象
        step = ModelStepProposal(tool_name="a", arguments={})
        with pytest.raises(FrozenInstanceError):
            step.tool_name = "b"

    def test_step_proposal_only_four_fields(self):
        import dataclasses

        fields = [f.name for f in dataclasses.fields(ModelStepProposal)]
        assert fields == ["tool_name", "arguments", "decision_summary", "confidence"]

    def test_client_protocol_signature(self):
        import inspect

        assert inspect.signature(JsonModelClient.complete).parameters.get("payload") is not None
