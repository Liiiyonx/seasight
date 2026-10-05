"""策略守卫：确定性规则策略，代替模型做基线决策。

手册 5.6：规则策略先给出可验证的基线决策，大模型只做增强；
`runtime_config.model_available=False` 时规则模式继续工作。

`PolicyGuard` 是协议（可注入自定义实现），`RulePolicyGuard` 是首版
确定性实现：
- `check_tool`：角色必须在工具 allowed_roles 内；risk_level 达到
  `require_approval_risk` 阈值 → `requires_approval=True`（挂起审批）。
- `check_plan`：对整个计划的工具可用性与角色权限做聚合预检，
  产出计划级 PolicyDecision（记录 policy_version）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from app.services.agents.model import AgentRunRequest, RiskLevel, RuntimeConfig

if TYPE_CHECKING:
    from app.services.agents.tools import ToolDefinition, ToolRegistry
    from app.services.agents.model import RuleStep


@dataclass(frozen=True)
class PolicyDecision:
    """策略守卫的判定结果。

    - allow：是否放行
    - reason：业务理由（审计留痕，解密预算允许保存）
    - requires_approval：是否必须人工审批
    - risk_level：被判定动作的风险级别
    - policy_version：判定所用策略版本
    """

    allow: bool
    reason: str = ""
    requires_approval: bool = False
    risk_level: RiskLevel | None = None
    policy_version: str = ""

    @classmethod
    def allow_decision(
        cls,
        *,
        reason: str = "策略放行",
        requires_approval: bool = False,
        risk_level: RiskLevel | None = None,
        policy_version: str = "",
    ) -> "PolicyDecision":
        return cls(
            allow=True,
            reason=reason,
            requires_approval=requires_approval,
            risk_level=risk_level,
            policy_version=policy_version,
        )

    @classmethod
    def deny_decision(cls, reason: str, *, policy_version: str = "") -> "PolicyDecision":
        return cls(allow=False, reason=reason, policy_version=policy_version)


class PolicyGuard(Protocol):
    """策略守卫协议（可注入，见手册 3.6）。

    协议方法体做参数校验后抛 NotImplementedError：既保证形参被真实
    使用（静态守卫），又保持纯协议语义 —— 具体判定由注入的实现提供。
    """

    def check_plan(
        self,
        plan: list["RuleStep"],
        request: AgentRunRequest,
        tools: "ToolRegistry",
    ) -> PolicyDecision:
        if not isinstance(plan, list) or not plan:
            raise TypeError(f"plan 必须是非空 RuleStep 列表：{type(plan).__name__}")
        if not isinstance(request, AgentRunRequest):
            raise TypeError(f"request 必须是 AgentRunRequest：{type(request).__name__}")
        if not hasattr(tools, "get"):
            raise TypeError(f"tools 必须提供 get()：{type(tools).__name__}")
        raise NotImplementedError("PolicyGuard.check_plan 未实现")

    def check_tool(
        self,
        tool: "ToolDefinition",
        role: str,
        input_: dict[str, Any],
    ) -> PolicyDecision:
        if not hasattr(tool, "name"):
            raise TypeError(f"tool 必须是 ToolDefinition：{type(tool).__name__}")
        if not isinstance(role, str):
            raise TypeError(f"role 必须是 str：{type(role).__name__}")
        if not isinstance(input_, dict):
            raise TypeError(f"input_ 必须是 dict：{type(input_).__name__}")
        raise NotImplementedError("PolicyGuard.check_tool 未实现")


class RulePolicyGuard:
    """确定性规则策略守卫（首版默认实现）。"""

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        blocked_tools: tuple[str, ...] = (),
    ) -> None:
        self.config = config or RuntimeConfig()
        self.blocked_tools = frozenset(blocked_tools)

    @property
    def policy_version(self) -> str:
        return self.config.policy_version

    def check_tool(
        self,
        tool: "ToolDefinition",
        role: str,
        input_: dict[str, Any],
    ) -> PolicyDecision:
        if tool.name in self.blocked_tools:
            return PolicyDecision.deny_decision(
                f"策略黑名单禁止调用 {tool.name}",
                policy_version=self.policy_version,
            )
        if role not in tool.allowed_roles:
            return PolicyDecision.deny_decision(
                f"角色 {role} 无权调用 {tool.name}（允许角色 {list(tool.allowed_roles)}）",
                policy_version=self.policy_version,
            )
        if not isinstance(input_, dict):
            return PolicyDecision.deny_decision(
                f"工具 {tool.name} 输入必须是对象：{type(input_).__name__}",
                policy_version=self.policy_version,
            )
        risk = tool.risk_level if isinstance(tool.risk_level, RiskLevel) else RiskLevel(tool.risk_level)
        needs_approval = risk in self.config.require_approval_risk
        # 决策理由带输入键摘要（解密预算：只留键名，不落原始载荷）
        input_keys = ",".join(sorted(input_)) or "(无参数)"
        return PolicyDecision.allow_decision(
            reason=f"角色 {role} 调用 {tool.name} 通过策略守卫（输入 {input_keys}）",
            requires_approval=needs_approval,
            risk_level=risk,
            policy_version=self.policy_version,
        )

    def check_plan(
        self,
        plan: list["RuleStep"],
        request: AgentRunRequest,
        tools: "ToolRegistry",
    ) -> PolicyDecision:
        denied: list[str] = []
        for step in plan:
            tool = tools.get(step.tool)
            if tool is None:
                denied.append(f"{step.tool} 未注册")
                continue
            if step.tool in self.blocked_tools:
                denied.append(f"{step.tool} 在黑名单中")
                continue
            if request.role not in tool.allowed_roles:
                denied.append(
                    f"{step.tool} 不允许角色 {request.role}（允许 {list(tool.allowed_roles)}）"
                )
        if denied:
            return PolicyDecision.deny_decision(
                "计划被策略守卫拒绝：" + "; ".join(denied),
                policy_version=self.policy_version,
            )
        return PolicyDecision.allow_decision(
            reason=f"计划 {len(plan)} 步全部通过策略守卫（角色 {request.role}）",
            policy_version=self.policy_version,
        )
