"""确定性规则规划器（规则模式，无大模型）。

规划来源：`runtime_config.rule_plan`（可注入）；为 None 时使用内置
`DEFAULT_RULE_PLAN`（对应计划书 5.5 的派单工具链：读事件 → 查设备 →
生成方案 → 建单/合并 → 观察执行）。

- 占位符绑定：步骤 input 里的 "{var}" 由请求参数与已完成的工具输出
  确定性地替换（无模型、无随机）。
- 期望验证：每个步骤可声明 Expectation（确定性业务规则），
  失败时按 on_violation 选择安全终止或重规划。
- 每次重规划必须新增 replan 与后续 plan 步骤，不得覆盖历史步骤
  （由 AgentRuntime._replan 保证，本模块只负责生成计划对象）。

★ 不在任何地方保存思维链：summary 是写给审计看的决策摘要，
input/output 只以哈希与摘要形式进入轨迹。
"""

from __future__ import annotations

import re
from typing import Any

from app.services.agents.errors import AgentError, ErrorCode
from app.services.agents.model import AgentRunRequest, Expectation, RuleStep, RuntimeConfig

# 内置派单管道（对应计划书 5.5 工具表的主链路）
DEFAULT_RULE_PLAN: list[dict[str, Any]] = [
    {
        "tool": "event.get",
        "input": {"event_id": "{event_id}"},
        "summary": "读取事件与证据",
        "expect": {"key": "event_id", "error_code": "tool_failed"},
    },
    {
        "tool": "device.query_available",
        "input": {"event_id": "{event_id}"},
        "summary": "查询可用机器人",
        "expect": {
            "key": "candidates",
            "min_items": 1,
            "error_code": "no_robot_available",
        },
    },
    {
        "tool": "dispatch.plan",
        "input": {"event_id": "{event_id}", "candidates": "{candidates}"},
        "summary": "生成派单方案",
        "expect": {"key": "robot_id", "error_code": "no_robot_available"},
    },
    {
        "tool": "task.create_or_merge",
        "input": {"event_id": "{event_id}", "robot_id": "{robot_id}"},
        "summary": "创建或合并任务（幂等）",
        "expect": {"key": "task_id", "error_code": "tool_failed"},
    },
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
    },
]

# 只支持整串占位符 "{var}"；字面字符串原样保留
_PLACEHOLDER_RE = re.compile(r"^\{([a-zA-Z0-9_]+)\}$")


def _bind_value(value: Any, bindings: dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = _PLACEHOLDER_RE.match(value)
        if match:
            key = match.group(1)
            if key not in bindings:
                raise AgentError(
                    f"计划步骤缺少绑定变量 {key}（可用：{sorted(bindings)}）",
                    code=ErrorCode.INVALID_TOOL_INPUT,
                )
            return bindings[key]
        return value
    if isinstance(value, dict):
        return {k: _bind_value(v, bindings) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bind_value(v, bindings) for v in value]
    return value


class RulePlanner:
    """规则规划器：把 rule_plan 配置解析为 RuleStep 列表并绑定输入。"""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    def build(self, request: AgentRunRequest) -> list[RuleStep]:
        """构建计划：把选中的规则计划解析为 RuleStep 列表。

        计划选择顺序：`request.plan_key` 命中 `RuntimeConfig.rule_plans` →
        `RuntimeConfig.rule_plan` → 内置 `DEFAULT_RULE_PLAN`。
        计划里可带 `role` 字段标注产出该步骤的智能体角色（多角色协同）。
        """
        raw = self._select_plan(request)
        steps: list[RuleStep] = []
        for item in raw:
            if not isinstance(item, dict) or "tool" not in item:
                raise AgentError(
                    f"非法规则计划项：{item}（必须包含 tool 字段）",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            expect_raw = item.get("expect")
            expect = Expectation(**expect_raw) if isinstance(expect_raw, dict) else None
            role = item.get("role")
            steps.append(
                RuleStep(
                    tool=str(item["tool"]),
                    input=dict(item.get("input") or {}),
                    summary=str(item.get("summary") or ""),
                    expect=expect,
                    on_error=str(item.get("on_error") or "terminate"),
                    role=str(role) if role else None,
                )
            )
        return steps

    def _select_plan(self, request: AgentRunRequest) -> list[dict[str, Any]]:
        """按请求选择计划：显式 plan_key 命中变体表，否则用配置的默认计划。

        ★ `plan_key=None`（默认）走的仍然是 `config.rule_plan`，
          与历史版本完全一致；变体表只是「额外可选的那几套计划」。
        """
        plan_key = request.plan_key
        if plan_key:
            variants = self.config.rule_plans or {}
            selected = variants.get(plan_key)
            if selected is None:
                # 显式点了不存在的计划必须响亮失败，不能静默回落成单角色：
                # 否则前端选了「多角色协同」却跑出单人轨迹，是比报错更糟的失真。
                raise AgentError(
                    f"未知的计划变体 {plan_key!r}（可用：{sorted(variants)}）",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            return selected
        if self.config.rule_plan is not None:
            return self.config.rule_plan
        return DEFAULT_RULE_PLAN

    def bind(self, step: RuleStep, bindings: dict[str, Any]) -> dict[str, Any]:
        """把占位符替换为绑定值（请求参数 + 已完成的工具输出）。"""
        return _bind_value(step.input, bindings)
