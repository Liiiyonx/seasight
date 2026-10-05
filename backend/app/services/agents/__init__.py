"""探海灵眸 Agent 内核（WP-01）。

模块定位（计划书 5.6 / 执行手册 3.6）：
    确定性控制器负责状态、规则、工具调用、超时、重试和终止；
    规则策略先给出可验证的基线决策；大模型仅作为可选增强，
    模型不可用时自动退回规则模式，核心能力不下降。

首版为纯内存确定性内核：不依赖数据库 / Redis / MQTT / 外网 / 大模型。
WP-02 以本包的数据结构为字段真源落 ORM 与迁移。
"""

from __future__ import annotations

from app.services.agents.errors import (
    ERROR_CODES,
    AgentError,
    AgentNotFoundError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    ErrorCode,
    InternalAgentError,
    InvalidToolInputError,
    InvalidToolOutputError,
    MaxStepsExceededError,
    PolicyDeniedError,
    RunExpiredError,
    TaskConflictError,
    ToolFailedError,
    ToolTimeoutError,
    is_error_code,
)
# WP-16 集成：可选仓储工厂（默认内存，engine 可选）
from app.services.agents.factory import (
    create_approval_repository,
    create_repositories,
    create_run_repository,
)
from app.services.agents.memory import (
    FORBIDDEN_MEMORY_KEYS,
    MEMORY_TYPES,
    InMemoryMemoryStore,
    MemoryEntry,
    MemoryStore,
)
# 跨 run 经验（lessons）：确定性「可进化」闭环，默认由 RuntimeConfig 控制开关
from app.services.agents.lessons import (
    LESSON_KINDS,
    LESSON_MEMORY_TYPE,
    LESSON_SCOPE_TYPE,
    Lesson,
    LessonStore,
    summarize_hits,
)
from app.services.agents.model_adapter import (
    JsonModelClient,
    ModelAdapterError,
    ModelAdapterPlanner,
    ModelPlanProposal,
    ModelSchemaError,
    ModelStepProposal,
    ModelTimeoutError,
    ModelUnavailableError,
    OpenAICompatibleModelClient,
    summarize_tools,
)
from app.services.agents.model import (
    TERMINAL_STATUSES,
    AgentRun,
    AgentRunRequest,
    AgentRunResult,
    AgentStatus,
    AgentStep,
    AgentStepType,
    Expectation,
    LEGAL_TRANSITIONS,
    RiskLevel,
    RuleStep,
    RuntimeConfig,
    RuntimeStatus,
)
from app.services.agents.planner import DEFAULT_RULE_PLAN, RulePlanner
from app.services.agents.policy import PolicyDecision, PolicyGuard, RulePolicyGuard
from app.services.agents.repositories import (
    ApprovalRecord,
    ApprovalRepository,
    InMemoryApprovalRepository,
    InMemoryRunRepository,
    RunRepository,
)
# WP-10 持久化仓储（可选；内存仓储仍是默认行为）
from app.services.agents.persistent_repository import (
    SqlAlchemyApprovalRepository,
    SqlAlchemyRunRepository,
)
from app.services.agents.runtime import (
    AgentRuntime,
    Clock,
    FakeClock,
    IdFactory,
    SequenceIdFactory,
    SystemClock,
    UuidIdFactory,
)
from app.services.agents.schema import canonical_json, is_valid, sha256_hex, validate_schema
from app.services.agents.tools import (
    ToolContext,
    ToolDefinition,
    ToolExecutor,
    ToolHandler,
    ToolRegistry,
    ToolResult,
)

__all__ = [
    # 冻结公开接口（手册 3.6）
    "AgentRuntime",
    "AgentRun",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentStep",
    "AgentStatus",
    "AgentStepType",
    "ToolRegistry",
    "ToolDefinition",
    "ToolContext",
    "ToolResult",
    "PolicyGuard",
    "PolicyDecision",
    "MemoryStore",
    "RunRepository",
    "ApprovalRepository",
    "AgentError",
    # WP-11 模型适配层冻结接口
    "JsonModelClient",
    "ModelStepProposal",
    "ModelPlanProposal",
    "ModelAdapterPlanner",
    "ModelAdapterError",
    "ModelUnavailableError",
    "ModelTimeoutError",
    "ModelSchemaError",
    "summarize_tools",
    "OpenAICompatibleModelClient",
    # WP-10 持久化仓储（可选；默认仍为内存仓储）
    "SqlAlchemyRunRepository",
    "SqlAlchemyApprovalRepository",
    # WP-16 集成：可选仓储工厂
    "create_run_repository",
    "create_approval_repository",
    "create_repositories",
    # 扩展
    "ErrorCode",
    "ERROR_CODES",
    "is_error_code",
    "RiskLevel",
    "RuntimeStatus",
    "RuntimeConfig",
    "RuleStep",
    "Expectation",
    "TERMINAL_STATUSES",
    "LEGAL_TRANSITIONS",
    "RulePlanner",
    "DEFAULT_RULE_PLAN",
    "RulePolicyGuard",
    "ToolExecutor",
    "ToolHandler",
    "MemoryEntry",
    "InMemoryMemoryStore",
    "FORBIDDEN_MEMORY_KEYS",
    "MEMORY_TYPES",
    # 跨 run 经验（lessons）
    "Lesson",
    "LessonStore",
    "LESSON_KINDS",
    "LESSON_MEMORY_TYPE",
    "LESSON_SCOPE_TYPE",
    "summarize_hits",
    "ApprovalRecord",
    "InMemoryApprovalRepository",
    "InMemoryRunRepository",
    "Clock",
    "IdFactory",
    "SystemClock",
    "FakeClock",
    "SequenceIdFactory",
    "UuidIdFactory",
    "canonical_json",
    "sha256_hex",
    "validate_schema",
    "is_valid",
    "AgentNotFoundError",
    "TaskConflictError",
    "ToolTimeoutError",
    "ToolFailedError",
    "PolicyDeniedError",
    "ApprovalRejectedError",
    "ApprovalTimeoutError",
    "InvalidToolInputError",
    "InvalidToolOutputError",
    "MaxStepsExceededError",
    "RunExpiredError",
    "InternalAgentError",
]
