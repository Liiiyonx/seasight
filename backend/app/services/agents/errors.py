"""Agent 内核结构化错误。

错误码清单与 Harness 执行手册 3.3 完全一致（12 个，禁止增删改），
由 `backend/tests/test_agent_runtime.py` 中的「错误码枚举对账守卫」锁定。

约定：
- `AgentError` 是所有内核异常的基类。
- `code` 取冻结错误码字符串；`code=None` 表示**调用侧**错误
  （如 run 不存在），这类错误不会被写入运行轨迹，也不会成为
  run 的 `termination_reason`。
- 运行结果中的 `termination_reason` / 步骤 `error_code` 只允许出现
  冻结错误码或状态值本身（succeeded / cancelled），由对账守卫测试校验。
"""

from __future__ import annotations

from typing import Final

# ----------------------------------------------------------------------
# 冻结错误码（手册 3.3）—— 与测试中的对账守卫同源
# ----------------------------------------------------------------------
ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
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
    }
)


class ErrorCode:
    """结构化错误码常量（与 ERROR_CODES 一一对应）。"""

    NO_ROBOT_AVAILABLE = "no_robot_available"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_FAILED = "tool_failed"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_TIMEOUT = "approval_timeout"
    TASK_CONFLICT = "task_conflict"
    INVALID_TOOL_INPUT = "invalid_tool_input"
    INVALID_TOOL_OUTPUT = "invalid_tool_output"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    RUN_EXPIRED = "run_expired"
    INTERNAL_ERROR = "internal_error"


def is_error_code(value: str | None) -> bool:
    """判断字符串是否为冻结错误码。"""
    return value in ERROR_CODES


class AgentError(Exception):
    """Agent 内核异常基类。

    `code` 必须在 `ERROR_CODES` 内；`None` 表示调用侧错误
    （不会进入运行轨迹）。
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code
        super().__init__(message)


class AgentNotFoundError(AgentError):
    """run / 审批 / 记忆对象不存在（调用侧错误，无冻结错误码）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=None)


class TaskConflictError(AgentError):
    """唯一约束冲突 / 幂等键冲突（run_id、step_id、(run_id, step_no)、
    memory_id、approval_id、幂等键）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.TASK_CONFLICT)


class ToolTimeoutError(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.TOOL_TIMEOUT)


class ToolFailedError(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.TOOL_FAILED)


class PolicyDeniedError(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.POLICY_DENIED)


class ApprovalRejectedError(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.APPROVAL_REJECTED)


class ApprovalTimeoutError(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.APPROVAL_TIMEOUT)


class InvalidToolInputError(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.INVALID_TOOL_INPUT)


class InvalidToolOutputError(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.INVALID_TOOL_OUTPUT)


class MaxStepsExceededError(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.MAX_STEPS_EXCEEDED)


class RunExpiredError(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.RUN_EXPIRED)


class InternalAgentError(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.INTERNAL_ERROR)
