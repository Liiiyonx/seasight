"""统一响应结构与应用异常。

约定：
- HTTP 状态码统一 200（业务错误通过 code 判断）
- 系统级错误（未捕获异常、验证失败）才用 4xx/5xx
- 所有响应带 trace_id 便于排查
"""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一响应体。"""

    code: int = Field(default=0, description="0=成功，非 0=业务错误码")
    message: str = Field(default="ok", description="提示信息")
    data: T | None = Field(default=None, description="业务数据")
    trace_id: str | None = Field(default=None, description="链路追踪 ID")

    @classmethod
    def ok(cls, data: T | None = None, message: str = "ok") -> "ApiResponse[T]":
        return cls(code=0, message=message, data=data)

    @classmethod
    def fail(cls, code: int, message: str, data: T | None = None) -> "ApiResponse[T]":
        return cls(code=code, message=message, data=data)


class ErrorCode:
    """业务错误码定义（按模块分段，便于定位）。"""

    # 通用 1xxx
    UNKNOWN = 1000
    PARAM_INVALID = 1001
    NOT_FOUND = 1002
    UNAUTHORIZED = 1003
    FORBIDDEN = 1004
    CONFLICT = 1005

    # 设备 2xxx
    DEVICE_NOT_FOUND = 2001
    DEVICE_OFFLINE = 2002
    DEVICE_SEQ_DUPLICATE = 2003

    # 事件 3xxx
    EVENT_NOT_FOUND = 3001
    EVENT_DUPLICATE = 3002

    # 任务 4xxx
    TASK_NOT_FOUND = 4001
    TASK_INVALID_TRANSITION = 4002       # 非法状态跳转
    TASK_NO_ROBOT_AVAILABLE = 4003       # 无可用机器人
    TASK_ACK_TIMEOUT = 4004

    # AI 5xxx
    AI_SERVICE_UNAVAILABLE = 5001
    AI_INFERENCE_FAILED = 5002

    # Agent 6xxx 与知识智能体 7xxx 的公开错误码分别由
    # app.api.v1.agents 和 app.services.knowledge 冻结，避免在此处重复真源。


class AppException(Exception):
    """业务异常基类。抛出后由全局处理器转为统一响应。"""

    def __init__(
        self,
        code: int = ErrorCode.UNKNOWN,
        message: str = "服务器内部错误",
        *,
        http_status: int = 200,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail or {}
        super().__init__(message)


class NotFoundError(AppException):
    def __init__(self, message: str = "资源不存在", code: int = ErrorCode.NOT_FOUND) -> None:
        super().__init__(code=code, message=message, http_status=404)


class ConflictError(AppException):
    def __init__(self, message: str = "资源冲突", code: int = ErrorCode.CONFLICT) -> None:
        super().__init__(code=code, message=message, http_status=409)


class InvalidStateTransitionError(AppException):
    """任务状态机非法跳转。"""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(
            code=ErrorCode.TASK_INVALID_TRANSITION,
            message=f"任务状态不允许从 {current} 变更为 {target}",
            http_status=200,
            detail={"current": current, "target": target},
        )


class NoRobotAvailableError(AppException):
    def __init__(self, reason: str = "无满足条件的可用机器人") -> None:
        super().__init__(
            code=ErrorCode.TASK_NO_ROBOT_AVAILABLE,
            message=reason,
            http_status=200,
        )
