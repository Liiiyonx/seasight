"""工具契约与执行链。

手册 3.5 冻结的工具定义字段（8 个）+ handler 实现：
    name / version / description / input_schema / output_schema /
    risk_level / timeout_ms / idempotent / allowed_roles

工具调用链（手册 3.5，顺序不可变）：
    参数 Schema 校验
    -> 角色与策略守卫
    -> 幂等检查
    -> 超时控制
    -> 执行
    -> 输出 Schema 校验
    -> 审计和轨迹

其中「审计和轨迹」由 AgentRuntime 通过步骤记录与 `audit` 回调完成，
本模块的 `ToolExecutor.execute_once` 完成前面 6 步（单次尝试）；
「重试上限」由 AgentRuntime 驱动（每次尝试都落一条 tool_call 步骤，
保证审计可追溯每一次尝试）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol

from app.services.agents.errors import (
    AgentError,
    ErrorCode,
    TaskConflictError,
    ToolFailedError,
)
from app.services.agents.model import RiskLevel
from app.services.agents.schema import is_valid, sha256_hex, validate_schema

if TYPE_CHECKING:
    from app.services.agents.policy import PolicyDecision, PolicyGuard

# ----------------------------------------------------------------------
# 工具定义
# ----------------------------------------------------------------------


class ToolContext:
    """传给工具 handler 的执行上下文（最小集合，可注入时钟）。"""

    def __init__(
        self,
        *,
        run_id: str,
        tool_name: str,
        role: str,
        input: dict[str, Any],  # noqa: A002  # 与手册「工具输入」语义一致
        clock: Any = None,
    ) -> None:
        self.run_id = run_id
        self.tool_name = tool_name
        self.role = role
        self.input = input
        self.clock = clock

    def now_ms(self) -> int:
        return self.clock.now_ms() if self.clock is not None else 0


ToolHandler = Callable[[ToolContext], dict[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    """工具契约（冻结字段，8+1 个）。"""

    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_level: RiskLevel | str
    timeout_ms: int
    idempotent: bool
    allowed_roles: tuple[str, ...]
    handler: ToolHandler | None = None


@dataclass
class ToolResult:
    """一次工具调用（单次尝试）的结果。"""

    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int = 0
    replayed: bool = False
    output_hash: str | None = None


# ----------------------------------------------------------------------
# 工具注册表
# ----------------------------------------------------------------------


class ToolRegistry:
    """工具注册表：注册 / 查询 / 枚举，并为幂等工具保存成功结果缓存。

    `idempotency_store` 可注入（默认内部 dict），同一 (工具, 输入哈希)
    的成功结果可跨 run 复用 —— 对应「重复触发幂等：复用 run 或任务」。
    """

    def __init__(
        self,
        *,
        idempotency_store: dict[tuple[str, str], dict[str, Any]] | None = None,
        idempotency_enabled: bool = True,
    ) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._idem_store: dict[tuple[str, str], dict[str, Any]] = (
            idempotency_store if idempotency_store is not None else {}
        )
        self._idem_enabled = idempotency_enabled

    def register(self, tool: ToolDefinition, *, overwrite: bool = False) -> None:
        if tool.name in self._tools and not overwrite:
            raise TaskConflictError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def require(self, name: str) -> ToolDefinition:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolFailedError(f"工具未注册：{name}")
        return tool

    def list(self) -> list[ToolDefinition]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def __len__(self) -> int:
        return len(self._tools)

    # ---- 幂等缓存 ----
    def idem_key(self, tool: ToolDefinition, input_: dict[str, Any]) -> tuple[str, str]:
        return tool.name, sha256_hex(input_)

    def idem_get(self, tool: ToolDefinition, input_: dict[str, Any]) -> ToolResult | None:
        if not (self._idem_enabled and tool.idempotent):
            return None
        cached = self._idem_store.get(self.idem_key(tool, input_))
        if cached is None:
            return None
        return ToolResult(
            ok=True,
            data=cached["data"],
            latency_ms=0,
            replayed=True,
            output_hash=cached["output_hash"],
        )

    def idem_put(self, tool: ToolDefinition, input_: dict[str, Any], data: dict[str, Any]) -> None:
        if not (self._idem_enabled and tool.idempotent):
            return
        self._idem_store[self.idem_key(tool, input_)] = {
            "data": data,
            "output_hash": sha256_hex(data),
        }


# ----------------------------------------------------------------------
# 工具执行器（单次尝试，完整调用链）
# ----------------------------------------------------------------------


class ToolExecutor:
    """执行单次工具调用，按手册 3.5 的顺序完成校验与守卫。

    `audit` 回调在成功执行后触发，接收调用信息字典（审计留痕）。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        policy_guard: "PolicyGuard",
        clock: Any = None,
        *,
        audit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.registry = registry
        self.policy_guard = policy_guard
        self.clock = clock
        self.audit = audit

    def execute_once(
        self,
        run_id: str,
        role: str,
        tool_name: str,
        input_: dict[str, Any],
    ) -> ToolResult:
        started = self.clock.now_ms() if self.clock is not None else 0

        def elapsed() -> int:
            return (self.clock.now_ms() if self.clock is not None else 0) - started

        try:
            tool = self.registry.require(tool_name)
        except AgentError as exc:
            return ToolResult(
                ok=False,
                error_code=exc.code or ErrorCode.TOOL_FAILED,
                error_message=str(exc),
                latency_ms=elapsed(),
            )

        # 1. 参数 Schema 校验
        input_errors = validate_schema(input_, tool.input_schema)
        if input_errors:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.INVALID_TOOL_INPUT,
                error_message=f"工具 {tool.name} 输入不合法：" + "; ".join(input_errors),
                latency_ms=elapsed(),
            )

        # 2. 角色与策略守卫
        decision: PolicyDecision = self.policy_guard.check_tool(tool, role, input_)
        if not decision.allow:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.POLICY_DENIED,
                error_message=decision.reason or f"策略拒绝调用 {tool.name}",
                latency_ms=elapsed(),
            )

        # 3. 幂等检查（成功结果缓存命中 → 重放，不再执行）
        cached = self.registry.idem_get(tool, input_)
        if cached is not None:
            cached.latency_ms = elapsed()
            return cached

        # 4. 超时控制 + 5. 执行
        if tool.handler is None:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.TOOL_FAILED,
                error_message=f"工具 {tool.name} 没有实现 handler",
                latency_ms=elapsed(),
            )
        try:
            ctx = ToolContext(
                run_id=run_id,
                tool_name=tool.name,
                role=role,
                input=input_,
                clock=self.clock,
            )
            data = tool.handler(ctx)
            if not isinstance(data, dict):
                raise ToolFailedError(f"工具 {tool.name} 返回类型必须是 dict，实际 {type(data).__name__}")
        except AgentError as exc:
            return ToolResult(
                ok=False,
                error_code=exc.code or ErrorCode.TOOL_FAILED,
                error_message=str(exc),
                latency_ms=elapsed(),
            )
        except Exception as exc:  # noqa: BLE001 —— 工具异常统一结构化，不让内核崩溃
            return ToolResult(
                ok=False,
                error_code=ErrorCode.TOOL_FAILED,
                error_message=f"工具 {tool.name} 执行异常：{exc}",
                latency_ms=elapsed(),
            )

        latency = elapsed()
        if latency > tool.timeout_ms:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.TOOL_TIMEOUT,
                error_message=f"工具 {tool.name} 超时（{latency}ms > {tool.timeout_ms}ms）",
                latency_ms=latency,
            )

        # 6. 输出 Schema 校验
        output_errors = validate_schema(data, tool.output_schema)
        if output_errors:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.INVALID_TOOL_OUTPUT,
                error_message=f"工具 {tool.name} 输出不合法：" + "; ".join(output_errors),
                latency_ms=latency,
            )

        # 7. 幂等缓存成功结果 + 审计和轨迹
        self.registry.idem_put(tool, input_, data)
        if self.audit is not None:
            self.audit(
                {
                    "run_id": run_id,
                    "tool_name": tool.name,
                    "tool_version": tool.version,
                    "role": role,
                    "input_hash": sha256_hex(input_),
                    "output_hash": sha256_hex(data),
                    "latency_ms": latency,
                    "ok": True,
                }
            )
        return ToolResult(
            ok=True,
            data=data,
            latency_ms=latency,
            output_hash=sha256_hex(data),
        )
