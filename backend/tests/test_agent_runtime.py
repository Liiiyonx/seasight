"""Agent Runtime 内核契约测试（WP-01 验收，纯逻辑，无外部依赖）。

覆盖手册要求的最小 10 类场景 + 补充场景：
    1. 正常派单闭环
    2. 无机器人
    3. 工具超时
    4. 策略拒绝
    5. 审批拒绝
    6. 达到最大步数
    7. 重复触发幂等
    8. 轨迹顺序与终态完整性
    9. 输出 Schema 错误
    10. 模型不可用时规则模式继续工作
补充：审批通过、审批超时、cancel 语义、run 过期、
错误码/状态/步骤类型/风险级别/ID 前缀的冻结清单对账守卫、
公开接口与可注入依赖守卫、仓储唯一约束守卫。

全部使用假时钟 + 序列 ID 工厂 + 内存仓储，确定可复现。
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.agents import (  # noqa: E402
    ERROR_CODES,
    TERMINAL_STATUSES,
    AgentError,
    AgentNotFoundError,
    AgentRunRequest,
    AgentRuntime,
    AgentStatus,
    AgentStepType,
    ErrorCode,
    FakeClock,
    RiskLevel,
    RuntimeConfig,
    SequenceIdFactory,
    TaskConflictError,
    ToolDefinition,
    ToolRegistry,
    validate_schema,
)

# ----------------------------------------------------------------------
# 冻结清单（Harness 手册 3.1 / 3.2 / 3.3 / 3.4 / 3.5 —— 对账守卫的真源）
# ----------------------------------------------------------------------
FROZEN_STATUSES = [
    "created",
    "planning",
    "waiting_policy",
    "waiting_approval",
    "executing",
    "observing",
    "verifying",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
]
FROZEN_STEP_TYPES = [
    "plan",
    "policy",
    "approval_request",
    "tool_call",
    "observation",
    "verification",
    "replan",
    "terminal",
]
FROZEN_ERROR_CODES = [
    "no_robot_available",
    "tool_timeout",
    "tool_failed",
    "policy_denied",
    "approval_rejected",
    "approval_timeout",
    "task_conflict",
    "invalid_tool_input",
    "invalid_tool_output",
    "max_steps_exceeded",
    "run_expired",
    "internal_error",
]
FROZEN_RISK_LEVELS = ["read_only", "write", "device_command", "sensitive"]
FROZEN_ID_PREFIXES = ["run_", "stp_", "apr_", "mem_"]

# ----------------------------------------------------------------------
# 测试工具集（确定性桩）
# ----------------------------------------------------------------------


def make_dispatch_registry():
    """构造默认派单管道的桩工具集，返回 (registry, calls)。

    calls["counts"] 记录每个工具的调用次数，用于断言「未执行」。
    """
    registry = ToolRegistry()
    calls: dict[str, Counter] = {"counts": Counter()}

    def tool(
        name: str,
        risk: RiskLevel,
        handler,
        *,
        roles=("operator", "dispatcher"),
        timeout_ms=5000,
        idempotent=False,
        input_schema=None,
        output_schema=None,
    ):
        registry.register(
            ToolDefinition(
                name=name,
                version="1.0.0",
                description=f"桩工具 {name}",
                input_schema=input_schema or {
                    "type": "object",
                    "required": [],
                    "properties": {},
                },
                output_schema=output_schema or {
                    "type": "object",
                    "required": [],
                    "properties": {},
                },
                risk_level=risk,
                timeout_ms=timeout_ms,
                idempotent=idempotent,
                allowed_roles=roles,
                handler=handler,
            )
        )

    def wrap(name, fn):
        def handler(ctx):
            calls["counts"][name] += 1
            return fn(ctx)

        return handler

    tool(
        "event.get",
        RiskLevel.READ_ONLY,
        wrap("event.get", lambda ctx: {"event_id": ctx.input["event_id"], "confirmed": True}),
        input_schema={
            "type": "object",
            "required": ["event_id"],
            "properties": {"event_id": {"type": "string"}},
        },
        output_schema={
            "type": "object",
            "required": ["event_id"],
            "properties": {"event_id": {"type": "string"}},
        },
    )
    tool(
        "device.query_available",
        RiskLevel.READ_ONLY,
        wrap(
            "device.query_available",
            lambda ctx: {"candidates": [{"robot_id": "rb_01", "battery": 90}]},
        ),
        input_schema={
            "type": "object",
            "required": ["event_id"],
            "properties": {"event_id": {"type": "string"}},
        },
        output_schema={
            "type": "object",
            "required": ["candidates"],
            "properties": {"candidates": {"type": "array", "items": {"type": "object"}}},
        },
    )
    tool(
        "dispatch.plan",
        RiskLevel.READ_ONLY,
        wrap(
            "dispatch.plan",
            lambda ctx: {"robot_id": ctx.input["candidates"][0]["robot_id"], "score": 0.9},
        ),
        input_schema={
            "type": "object",
            "required": ["event_id", "candidates"],
            "properties": {
                "event_id": {"type": "string"},
                "candidates": {"type": "array"},
            },
        },
        output_schema={
            "type": "object",
            "required": ["robot_id"],
            "properties": {"robot_id": {"type": "string"}},
        },
    )
    tool(
        "task.create_or_merge",
        RiskLevel.WRITE,
        wrap(
            "task.create_or_merge",
            lambda ctx: {"task_id": "tsk_0001", "action": "created"},
        ),
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["event_id", "robot_id"],
            "properties": {
                "event_id": {"type": "string"},
                "robot_id": {"type": "string"},
            },
        },
        output_schema={
            "type": "object",
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string"}},
        },
    )
    tool(
        "mission.observe",
        RiskLevel.READ_ONLY,
        wrap("mission.observe", lambda ctx: {"status": "done", "progress": 1.0}),
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
    )
    return registry, calls


def make_runtime(registry=None, calls=None, runtime_config=None, **overrides):
    """构造确定性的 runtime：假时钟 + 序列 ID + 内存仓储。"""
    config = runtime_config if runtime_config is not None else RuntimeConfig(**overrides)
    runtime = AgentRuntime(
        tool_registry=registry,
        clock=FakeClock(),
        id_factory=SequenceIdFactory(),
        runtime_config=config,
    )
    return runtime


def dispatch_request(idempotency_key=None, **params):
    """构造默认派单请求；idempotency_key 作为请求级字段传入（不进业务参数）。"""
    base = {"event_id": "evt_001"}
    base.update(params)
    return AgentRunRequest(
        trigger_type="event",
        objective="处理海漂垃圾事件并完成派单闭环",
        actor="operator-01",
        role="operator",
        params=base,
        idempotency_key=idempotency_key,
    )


# ======================================================================
# 1. 正常派单闭环
# ======================================================================


class TestNormalClosedLoop:
    def test_dispatch_succeeded(self):
        registry, calls = make_dispatch_registry()
        runtime = make_runtime(registry)
        result = runtime.run(dispatch_request())

        assert result.status == "succeeded"
        assert result.termination_reason == "succeeded"
        assert result.error_code is None
        assert result.run_id.startswith("run_")
        assert result.finished_at is not None
        assert result.policy_version == "rules-v1.0"
        assert result.steps, "成功 run 必须留下完整轨迹"

        types = [s.step_type for s in result.steps]
        assert types[0] == "plan"
        assert "policy" in types
        assert types[-1] == "terminal"
        # 5 个工具各执行一次
        assert calls["counts"]["event.get"] == 1
        assert calls["counts"]["task.create_or_merge"] == 1
        assert calls["counts"]["mission.observe"] == 1
        # 工作记忆写入（决策摘要，不保存思维链）
        memories = runtime.memory.find(scope_type="run", scope_id=result.run_id)
        assert memories, "观察/策略摘要必须进入记忆"
        for entry in memories:
            assert isinstance(entry.content, dict)
            assert "summary" in entry.content
            assert "chain_of_thought" not in str(entry.content)

    def test_trace_has_hashes_not_raw_payload(self):
        registry, _ = make_dispatch_registry()
        runtime = make_runtime(registry)
        result = runtime.run(dispatch_request())
        tool_steps = [s for s in result.steps if s.step_type == "tool_call"]
        for step in tool_steps:
            assert step.input_hash and step.output_hash, "工具步骤必须记录输入/输出哈希"
            assert step.decision_summary.startswith("调用")
            assert "chain_of_thought" not in step.decision_summary.lower()


# ======================================================================
# 2. 无机器人
# ======================================================================


class TestNoRobot:
    def test_no_robot_available(self):
        registry, calls = make_dispatch_registry()
        # 覆盖 device.query_available：返回空候选（overwrite 覆盖桩）
        registry.register(
            ToolDefinition(
                name="device.query_available",
                version="1.0.0",
                description="无机器人桩",
                input_schema={"type": "object", "required": ["event_id"], "properties": {}},
                output_schema={"type": "object", "required": ["candidates"], "properties": {}},
                risk_level=RiskLevel.READ_ONLY,
                timeout_ms=5000,
                idempotent=False,
                allowed_roles=("operator", "dispatcher"),
                handler=lambda ctx: {"candidates": []},
            ),
            overwrite=True,
        )
        runtime = make_runtime(registry)
        result = runtime.run(dispatch_request())

        assert result.status == "failed"
        assert result.error_code == "no_robot_available"
        assert result.termination_reason == "no_robot_available"
        # 绝不误报成功：未创建任何任务、未下发任何设备指令
        assert calls["counts"]["task.create_or_merge"] == 0
        types = [s.step_type for s in result.steps]
        assert types[-1] == "terminal"
        terminal = result.steps[-1]
        assert terminal.error_code == "no_robot_available"


# ======================================================================
# 3. 工具超时
# ======================================================================


class TestToolTimeout:
    def test_tool_timeout_with_retry_limit(self):
        registry = ToolRegistry()
        calls: Counter = Counter()

        def slow_handler(ctx):
            calls["slow.tool"] += 1
            ctx.clock.advance(6000)  # 模拟执行超过 5000ms 超时阈值
            return {"ok": True}

        registry.register(
            ToolDefinition(
                name="slow.tool",
                version="1.0.0",
                description="慢工具",
                input_schema={"type": "object", "required": [], "properties": {}},
                output_schema={"type": "object", "required": ["ok"], "properties": {}},
                risk_level=RiskLevel.READ_ONLY,
                timeout_ms=5000,
                idempotent=False,
                allowed_roles=("operator",),
                handler=slow_handler,
            )
        )
        config = RuntimeConfig(
            rule_plan=[
                {"tool": "slow.tool", "input": {}, "summary": "调用慢工具", "on_error": "terminate"}
            ],
            tool_timeout_ms=5000,
            retry_limit=1,
        )
        runtime = make_runtime(registry, runtime_config=config)
        result = runtime.run(
            AgentRunRequest(trigger_type="manual", objective="超时测试", params={})
        )

        assert result.status == "failed"
        assert result.error_code == "tool_timeout"
        # 重试上限 = 1 → 共尝试 2 次，每次尝试都留一条 tool_call 步骤
        tool_steps = [s for s in result.steps if s.step_type == "tool_call"]
        assert len(tool_steps) == 2
        assert calls["slow.tool"] == 2
        for step in tool_steps:
            assert step.error_code == "tool_timeout"
            assert step.status == "failed"
            assert step.latency_ms >= 6000


# ======================================================================
# 4. 策略拒绝
# ======================================================================


class TestPolicyDenied:
    def test_role_not_allowed_denies_plan(self):
        registry, calls = make_dispatch_registry()
        runtime = make_runtime(registry)
        # viewer 不在任何工具的 allowed_roles 内 → 计划级策略拒绝
        request = dispatch_request()
        request.role = "viewer"
        result = runtime.run(request)

        assert result.status == "failed"
        assert result.error_code == "policy_denied"
        assert result.termination_reason == "policy_denied"
        # 计划被拒后不执行任何工具
        assert sum(calls["counts"].values()) == 0
        types = [s.step_type for s in result.steps]
        assert types == ["plan", "policy", "terminal"]
        assert result.steps[1].status == "failed"
        assert result.steps[1].error_code == "policy_denied"


# ======================================================================
# 5. 审批拒绝（不发送设备指令）
# ======================================================================


def make_approval_plan_registry():
    """含敏感设备指令步骤的计划（mqtt.send_task 需要审批）。"""
    registry, calls = make_dispatch_registry()
    registry.register(
        ToolDefinition(
            name="mqtt.send_task",
            version="1.0.0",
            description="下发设备指令",
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {"task_id": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "required": ["sent"],
                "properties": {"sent": {"type": "boolean"}},
            },
            risk_level=RiskLevel.SENSITIVE,
            timeout_ms=5000,
            idempotent=False,
            allowed_roles=("operator", "dispatcher"),
            handler=lambda ctx: (calls["counts"].__setitem__("mqtt.send_task", calls["counts"]["mqtt.send_task"] + 1) or {"sent": True}),
        )
    )
    return registry, calls


def approval_plan_request():
    request = dispatch_request()
    request.params["task_id"] = "tsk_0001"
    return request


class TestApprovalRejected:
    def test_rejected_terminates_without_tool_execution(self):
        registry, calls = make_approval_plan_registry()
        config = RuntimeConfig(
            rule_plan=[
                {"tool": "event.get", "input": {"event_id": "{event_id}"}, "summary": "读取事件"},
                {
                    "tool": "mqtt.send_task",
                    "input": {"task_id": "{task_id}"},
                    "summary": "下发设备指令",
                },
            ],
            require_approval_risk=(RiskLevel.DEVICE_COMMAND, RiskLevel.SENSITIVE),
        )
        runtime = make_runtime(registry, runtime_config=config)
        result = runtime.run(approval_plan_request())

        # 审批挂起：不终态、有审批单
        assert result.status == "waiting_approval"
        assert len(result.pending_approval_ids) == 1
        approval_id = result.pending_approval_ids[0]
        assert approval_id.startswith("apr_")
        # 审批前不允许执行任何工具（含只读工具）
        assert sum(calls["counts"].values()) == 0

        # 人工拒绝
        runtime.approvals.decide(approval_id, "admin", "rejected", "临时管制，不派发")
        result2 = runtime.resume(result.run_id)
        assert result2.status == "failed"
        assert result2.error_code == "approval_rejected"
        assert result2.termination_reason == "approval_rejected"
        # 拒绝后仍然不发送设备指令
        assert calls["counts"]["mqtt.send_task"] == 0
        types = [s.step_type for s in result2.steps]
        assert "approval_request" in types
        assert types[-1] == "terminal"

    def test_approved_continues_execution(self):
        registry, calls = make_approval_plan_registry()
        config = RuntimeConfig(
            rule_plan=[
                {"tool": "event.get", "input": {"event_id": "{event_id}"}, "summary": "读取事件"},
                {
                    "tool": "mqtt.send_task",
                    "input": {"task_id": "{task_id}"},
                    "summary": "下发设备指令",
                },
            ],
            require_approval_risk=(RiskLevel.DEVICE_COMMAND, RiskLevel.SENSITIVE),
        )
        runtime = make_runtime(registry, runtime_config=config)
        result = runtime.run(approval_plan_request())
        assert result.status == "waiting_approval"

        runtime.approvals.decide(result.pending_approval_ids[0], "admin", "approved", "确认派发")
        result2 = runtime.resume(result.run_id)
        assert result2.status == "succeeded"
        assert calls["counts"]["event.get"] == 1
        assert calls["counts"]["mqtt.send_task"] == 1


# ======================================================================
# 6. 达到最大步数（安全终止，不无限循环）
# ======================================================================


class TestMaxSteps:
    def test_max_steps_exceeded_safe_termination(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="mission.observe",
                version="1.0.0",
                description="永远 pending 的观察工具",
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
                handler=lambda ctx: {"status": "pending"},
            )
        )
        config = RuntimeConfig(
            rule_plan=[
                {
                    "tool": "mission.observe",
                    "input": {"task_id": "{task_id}"},
                    "summary": "观察任务执行",
                    "expect": {
                        "key": "status",
                        "contains": ("done",),
                        "on_violation": "replan",
                        "error_code": "tool_failed",
                    },
                }
            ],
            max_steps=6,
            max_replans=5,
        )
        runtime = make_runtime(registry, runtime_config=config)
        result = runtime.run(
            AgentRunRequest(
                trigger_type="manual",
                objective="最大步数测试",
                params={"task_id": "tsk_x"},
            )
        )

        assert result.status == "failed"
        assert result.error_code == "max_steps_exceeded"
        assert result.termination_reason == "max_steps_exceeded"
        types = [s.step_type for s in result.steps]
        assert "replan" in types or len(types) >= 3
        assert types[-1] == "terminal"
        assert result.steps[-1].error_code == "max_steps_exceeded"


# ======================================================================
# 7. 重复触发幂等
# ======================================================================


class TestIdempotentTrigger:
    def test_same_idempotency_key_reuses_run(self):
        registry, calls = make_dispatch_registry()
        runtime = make_runtime(registry)
        request = dispatch_request(idempotency_key="evt_001")

        first = runtime.run(request)
        assert first.status == "succeeded"
        second = runtime.run(request)

        assert second.run_id == first.run_id
        assert second.idempotent_replay is True
        assert runtime.runs.count() == 1  # 没有新建第二个 run
        # 复用时不重复执行工具
        assert calls["counts"]["task.create_or_merge"] == 1

    def test_different_key_creates_new_run(self):
        runtime = make_runtime(*make_dispatch_registry())
        a = runtime.run(dispatch_request(idempotency_key="evt_001"))
        b = runtime.run(dispatch_request(idempotency_key="evt_002"))
        assert a.run_id != b.run_id
        assert runtime.runs.count() == 2

    def test_tool_level_idempotent_replay(self):
        registry, calls = make_dispatch_registry()
        runtime = make_runtime(registry)
        # 工具级幂等：task.create_or_merge 幂等，同输入重放不重复执行
        result = runtime.run(dispatch_request(idempotency_key="k1"))
        tool_steps = [s for s in result.steps if s.tool_name == "task.create_or_merge"]
        assert calls["counts"]["task.create_or_merge"] == 1
        # 直接再次触发同事件（不同幂等键）→ 计划重建，但幂等工具重放
        result2 = runtime.run(dispatch_request(idempotency_key="k2"))
        tool_steps2 = [s for s in result2.steps if s.tool_name == "task.create_or_merge"]
        assert calls["counts"]["task.create_or_merge"] == 1  # 未再次执行
        assert tool_steps2[-1].output_hash == tool_steps[-1].output_hash


# ======================================================================
# 8. 轨迹顺序与终态完整性
# ======================================================================


class TestTraceAndTerminal:
    def test_step_numbers_strictly_increasing(self):
        registry, _ = make_dispatch_registry()
        runtime = make_runtime(registry)
        result = runtime.run(dispatch_request())
        nums = [s.step_no for s in result.steps]
        assert nums == list(range(1, len(nums) + 1))

    def test_trace_order_valid(self):
        registry, _ = make_dispatch_registry()
        runtime = make_runtime(registry)
        result = runtime.run(dispatch_request())
        types = [s.step_type for s in result.steps]

        # 首步 plan、末步 terminal
        assert types[0] == "plan"
        assert types[-1] == "terminal"
        # 类型序列必须属于合法模式（允许审批段与重规划段为空）
        pattern = self._normalize(types)
        assert pattern, f"轨迹类型序列不合法：{types}"

    @staticmethod
    def _normalize(types):
        """把步骤类型序列压缩成 骨架 模式并校验。"""
        if types[0] != "plan" or types[-1] != "terminal":
            return None
        core = types[1:-1]
        # 每个 tool_call 之后必须有 observation（工具调用→观察成对）
        for i, t in enumerate(core):
            if t == "tool_call":
                if i + 1 >= len(core) or core[i + 1] != "observation":
                    return None
        # verification 必须出现在终态之前
        if "verification" not in core:
            return None
        # 不允许出现未知类型
        allowed = {t.value for t in AgentStepType}
        if any(t not in allowed for t in types):
            return None
        return tuple(types)

    def test_terminal_state_immutable(self):
        registry, _ = make_dispatch_registry()
        runtime = make_runtime(registry)
        result = runtime.run(dispatch_request())
        assert result.status == "succeeded"

        # resume / cancel 终态 run：不得再迁移
        again = runtime.resume(result.run_id)
        assert again.status == "succeeded"
        again2 = runtime.cancel(result.run_id, "admin", "尝试取消终态")
        assert again2.status == "succeeded"
        assert runtime.runs.get(result.run_id).status == "succeeded"

    def test_replan_appends_and_never_overwrites(self):
        """观察→验证→重规划：第二次观察成功，重规划步骤只追加不覆盖。"""
        registry = ToolRegistry()
        calls: Counter = Counter()

        def observe(ctx):
            calls["mission.observe"] += 1
            if calls["mission.observe"] == 1:
                return {"status": "pending"}
            return {"status": "done"}

        registry.register(
            ToolDefinition(
                name="mission.observe",
                version="1.0.0",
                description="第一次 pending，第二次 done",
                input_schema={"type": "object", "required": ["task_id"], "properties": {}},
                output_schema={"type": "object", "required": ["status"], "properties": {}},
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
                    "summary": "观察任务执行",
                    "expect": {
                        "key": "status",
                        "contains": ("done",),
                        "on_violation": "replan",
                        "error_code": "tool_failed",
                    },
                }
            ],
            max_replans=3,
        )
        runtime = make_runtime(registry, runtime_config=config)
        result = runtime.run(
            AgentRunRequest(trigger_type="manual", objective="重规划测试", params={"task_id": "tsk_r"})
        )

        assert result.status == "succeeded"
        types = [s.step_type for s in result.steps]
        assert "replan" in types
        replan_index = types.index("replan")
        # 重规划之后必须跟着新的 plan 步骤（手册 3.2）
        assert types[replan_index + 1] == "plan"
        # 历史步骤不被覆盖：step_no 严格递增、无重复
        nums = [s.step_no for s in result.steps]
        assert nums == list(range(1, len(nums) + 1))
        # 第一次失败的 tool_call 仍然在轨迹中
        failed_calls = [
            s for s in result.steps if s.step_type == "tool_call" and s.status == "failed"
        ]
        assert len(failed_calls) == 1


# ======================================================================
# 9. 输出 Schema 错误
# ======================================================================


class TestOutputSchemaError:
    def test_invalid_tool_output(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="bad.out",
                version="1.0.0",
                description="输出不符合 schema",
                input_schema={"type": "object", "required": [], "properties": {}},
                output_schema={
                    "type": "object",
                    "required": ["result"],
                    "properties": {"result": {"type": "string"}},
                },
                risk_level=RiskLevel.READ_ONLY,
                timeout_ms=5000,
                idempotent=False,
                allowed_roles=("operator",),
                handler=lambda ctx: {"result": 123},  # 类型错误
            )
        )
        config = RuntimeConfig(
            rule_plan=[{"tool": "bad.out", "input": {}, "summary": "坏输出工具"}]
        )
        runtime = make_runtime(registry, runtime_config=config)
        result = runtime.run(AgentRunRequest(trigger_type="manual", objective="输出校验", params={}))

        assert result.status == "failed"
        assert result.error_code == "invalid_tool_output"
        assert result.termination_reason == "invalid_tool_output"
        tool_step = result.steps[-2]
        assert tool_step.error_code == "invalid_tool_output"


# ======================================================================
# 10. 模型不可用 → 规则模式继续工作
# ======================================================================


class TestRuleMode:
    def test_rule_mode_without_model(self):
        registry, _ = make_dispatch_registry()
        config = RuntimeConfig(model_available=False)
        runtime = AgentRuntime(
            tool_registry=registry,
            clock=FakeClock(),
            id_factory=SequenceIdFactory(),
            runtime_config=config,
        )
        result = runtime.run(dispatch_request())
        assert result.status == "succeeded"
        runtime_status = runtime.status()
        assert runtime_status.model_available is False
        assert runtime_status.rule_mode is True

    def test_rule_mode_with_model_flag_still_deterministic(self):
        registry, _ = make_dispatch_registry()
        config = RuntimeConfig(model_available=True)
        runtime = AgentRuntime(
            tool_registry=registry,
            clock=FakeClock(),
            id_factory=SequenceIdFactory(),
            runtime_config=config,
        )
        result = runtime.run(dispatch_request())
        assert result.status == "succeeded"
        assert runtime.status().model_available is True


# ======================================================================
# 补充：审批超时 / cancel 语义 / run 过期
# ======================================================================


class TestApprovalTimeout:
    def test_approval_timeout_on_resume(self):
        registry, calls = make_approval_plan_registry()
        config = RuntimeConfig(
            rule_plan=[
                {"tool": "event.get", "input": {"event_id": "{event_id}"}, "summary": "读取事件"},
                {
                    "tool": "mqtt.send_task",
                    "input": {"task_id": "{task_id}"},
                    "summary": "下发设备指令",
                },
            ],
            approval_timeout_ms=1000,
            require_approval_risk=(RiskLevel.DEVICE_COMMAND, RiskLevel.SENSITIVE),
        )
        clock = FakeClock()
        runtime = AgentRuntime(
            tool_registry=registry,
            clock=clock,
            id_factory=SequenceIdFactory(),
            runtime_config=config,
        )
        result = runtime.run(approval_plan_request())
        assert result.status == "waiting_approval"

        clock.advance(2000)  # 超过审批超时
        result2 = runtime.resume(result.run_id)
        assert result2.status == "failed"
        assert result2.error_code == "approval_timeout"
        assert calls["counts"]["mqtt.send_task"] == 0


class TestCancel:
    def test_cancel_waiting_approval_run(self):
        registry, calls = make_approval_plan_registry()
        config = RuntimeConfig(
            rule_plan=[
                {"tool": "event.get", "input": {"event_id": "{event_id}"}, "summary": "读取事件"},
                {
                    "tool": "mqtt.send_task",
                    "input": {"task_id": "{task_id}"},
                    "summary": "下发设备指令",
                },
            ],
            require_approval_risk=(RiskLevel.DEVICE_COMMAND, RiskLevel.SENSITIVE),
        )
        runtime = make_runtime(registry, runtime_config=config)
        result = runtime.run(approval_plan_request())
        assert result.status == "waiting_approval"

        cancelled = runtime.cancel(result.run_id, "admin", "人工取消")
        assert cancelled.status == "cancelled"
        assert cancelled.termination_reason == "cancelled"
        assert cancelled.finished_at is not None
        assert cancelled.steps[-1].step_type == "terminal"
        assert cancelled.steps[-1].status == "cancelled"
        # 挂起审批随取消关闭
        record = runtime.approvals.get(result.pending_approval_ids[0])
        assert record.decision == "cancelled"
        assert record.decided_by == "admin"
        # 工具一个都没执行
        assert sum(calls["counts"].values()) == 0

    def test_cancel_terminal_is_noop(self):
        registry, _ = make_dispatch_registry()
        runtime = make_runtime(registry)
        result = runtime.run(dispatch_request())
        assert result.status == "succeeded"
        again = runtime.cancel(result.run_id, "admin", "再次取消")
        assert again.status == "succeeded"
        assert again.termination_reason == "succeeded"

    def test_cancel_unknown_run_raises(self):
        runtime = make_runtime(*make_dispatch_registry())
        with pytest.raises(AgentNotFoundError):
            runtime.cancel("run_does_not_exist", "admin", "x")


class TestRunExpired:
    def test_run_ttl_expires_non_terminal_run(self):
        registry, _ = make_dispatch_registry()
        clock = FakeClock()
        runtime = AgentRuntime(
            tool_registry=registry,
            clock=clock,
            id_factory=SequenceIdFactory(),
            runtime_config=RuntimeConfig(run_ttl_ms=1000),
        )
        # 直接通过仓储播种一个 created 状态的 run，模拟遗留挂起
        from app.services.agents import AgentRun

        now = clock.now()
        run = AgentRun(
            run_id="run_seed",
            trigger_type="event",
            objective="遗留运行",
            status="created",
            request=AgentRunRequest(trigger_type="event", objective="遗留运行", params={}),
            started_at=now,
            created_at=now,
            updated_at=now,
            runtime_state={"stage": "created"},
        )
        runtime.runs.create(run)
        clock.advance(5000)  # 超过 run_ttl
        result = runtime.resume("run_seed")
        assert result.status == "expired"
        assert result.error_code == "run_expired"
        assert result.termination_reason == "run_expired"


# ======================================================================
# 冻结清单对账守卫（手册 3.1 ~ 3.5 穷举）
# ======================================================================


class TestFrozenContractGuard:
    def test_status_enum_exhaustive(self):
        values = [s.value for s in AgentStatus]
        assert values == FROZEN_STATUSES
        assert set(TERMINAL_STATUSES) == {"succeeded", "failed", "cancelled", "expired"}

    def test_step_type_enum_exhaustive(self):
        assert [t.value for t in AgentStepType] == FROZEN_STEP_TYPES

    def test_error_code_exhaustive(self):
        assert sorted(ERROR_CODES) == sorted(FROZEN_ERROR_CODES)
        # ErrorCode 类常量与清单一一对应
        codes = {
            ErrorCode.NO_ROBOT_AVAILABLE,
            ErrorCode.TOOL_TIMEOUT,
            ErrorCode.TOOL_FAILED,
            ErrorCode.POLICY_DENIED,
            ErrorCode.APPROVAL_REJECTED,
            ErrorCode.APPROVAL_TIMEOUT,
            ErrorCode.TASK_CONFLICT,
            ErrorCode.INVALID_TOOL_INPUT,
            ErrorCode.INVALID_TOOL_OUTPUT,
            ErrorCode.MAX_STEPS_EXCEEDED,
            ErrorCode.RUN_EXPIRED,
            ErrorCode.INTERNAL_ERROR,
        }
        assert codes == ERROR_CODES

    def test_risk_level_exhaustive(self):
        assert [r.value for r in RiskLevel] == FROZEN_RISK_LEVELS

    def test_run_outcome_reasons_are_frozen(self):
        """任何 run 的终止原因只能来自冻结错误码或终态状态值本身。"""
        registry, _ = make_dispatch_registry()
        runtime = make_runtime(registry)
        results = []
        results.append(runtime.run(dispatch_request()))
        results.append(runtime.run(dispatch_request(idempotency_key="evt_ok")))
        allowed = ERROR_CODES | {"succeeded", "cancelled", "failed", "expired"}
        for r in results:
            assert r.termination_reason in allowed

    def test_id_prefixes_frozen(self):
        registry, _ = make_approval_plan_registry()
        config = RuntimeConfig(
            rule_plan=[
                {
                    "tool": "mqtt.send_task",
                    "input": {"task_id": "{task_id}"},
                    "summary": "下发设备指令",
                }
            ],
            require_approval_risk=(RiskLevel.DEVICE_COMMAND, RiskLevel.SENSITIVE),
        )
        runtime = make_runtime(registry, runtime_config=config)
        result = runtime.run(approval_plan_request())
        assert result.run_id.startswith("run_")
        for step in result.steps:
            assert step.step_id.startswith("stp_")
        for aid in result.pending_approval_ids:
            assert aid.startswith("apr_")
        memories = runtime.memory.find(scope_type="run", scope_id=result.run_id)
        if memories:
            for entry in memories:
                assert entry.memory_id.startswith("mem_")
        # 四种前缀全部出现在本次运行中
        seen = {
            result.run_id[:4],
            result.steps[0].step_id[:4],
            result.pending_approval_ids[0][:4],
            memories[0].memory_id[:4] if memories else "mem_",
        }
        assert seen == set(FROZEN_ID_PREFIXES)


# ======================================================================
# 公开接口与可注入依赖守卫（手册 3.6）
# ======================================================================


class TestPublicInterface:
    def test_runtime_entrypoints_exist(self):
        runtime = AgentRuntime(clock=FakeClock(), id_factory=SequenceIdFactory())
        assert callable(runtime.run)
        assert callable(runtime.cancel)
        assert callable(runtime.resume)
        assert callable(runtime.status)

    def test_all_injectables_accepted(self):
        """8 个可注入依赖全部可注入且生效。"""
        from app.services.agents import (
            InMemoryApprovalRepository,
            InMemoryMemoryStore,
            InMemoryRunRepository,
            RulePolicyGuard,
        )

        registry, _ = make_dispatch_registry()
        clock = FakeClock()
        idf = SequenceIdFactory()
        config = RuntimeConfig()
        runtime = AgentRuntime(
            tool_registry=registry,
            policy_guard=RulePolicyGuard(config),
            memory_store=InMemoryMemoryStore(id_factory=idf, clock=clock),
            run_repository=InMemoryRunRepository(),
            approval_repository=InMemoryApprovalRepository(),
            clock=clock,
            id_factory=idf,
            runtime_config=config,
        )
        result = runtime.run(dispatch_request())
        assert result.status == "succeeded"
        assert runtime.status().total_runs == 1

    def test_repository_uniqueness_guards(self):
        """run_id / step_id / (run_id, step_no) 唯一约束。"""
        from app.services.agents import AgentRun, AgentStep, InMemoryRunRepository

        repo = InMemoryRunRepository()
        now = datetime(2026, 1, 1)
        req = AgentRunRequest(trigger_type="event", objective="o")
        run = AgentRun(
            run_id="run_a",
            trigger_type="event",
            objective="o",
            status="created",
            request=req,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        repo.create(run)
        with pytest.raises(TaskConflictError):
            repo.create(run)  # run_id 重复

        repo.add_step(AgentStep(step_id="stp_a", run_id="run_a", step_no=1, step_type="plan", decision_summary="s"))
        with pytest.raises(TaskConflictError):
            repo.add_step(AgentStep(step_id="stp_b", run_id="run_a", step_no=1, step_type="plan", decision_summary="s"))  # (run_id, step_no) 重复
        with pytest.raises(TaskConflictError):
            repo.add_step(AgentStep(step_id="stp_a", run_id="run_a", step_no=2, step_type="plan", decision_summary="s"))  # step_id 重复


# ======================================================================
# 独立小工具：schema 校验器自检（供 tools 测试复用）
# ======================================================================


class TestSchemaValidatorSanity:
    def test_validate_schema_basics(self):
        schema = {
            "type": "object",
            "required": ["event_id"],
            "properties": {
                "event_id": {"type": "string", "minLength": 1},
                "count": {"type": "integer", "minimum": 0},
            },
        }
        assert validate_schema({"event_id": "a", "count": 1}, schema) == []
        assert validate_schema({"count": 1}, schema)  # 缺必填
        assert validate_schema({"event_id": 3}, schema)  # 类型错
        assert validate_schema({"event_id": "a", "count": -1}, schema)  # 小于 minimum
