"""Agent 内核领域模型：状态、步骤类型、风险级别、运行、步骤、结果。

★ 字段名与 Harness 执行手册 3.7 数据表**一一对齐**，供 WP-02 ORM
直接映射，语义不允许改动：

    t_agent_run       -> AgentRun（run_id/trigger_type/objective/status/
                         policy_version/started_at/finished_at/
                         termination_reason/trace_id/created_at/updated_at）
    t_agent_step      -> AgentStep（step_id/run_id/step_no/step_type/
                         decision_summary/tool_name/tool_version/input_hash/
                         output_hash/status/latency_ms/error_code/created_at）
    t_agent_approval  -> ApprovalRecord（见 repositories.py）
    t_agent_memory    -> MemoryEntry（见 memory.py）

唯一约束（手册 3.7）：
    run_id / step_id / memory_id / approval_id 唯一；
    (run_id, step_no) 唯一 —— 由内存仓储在 add_step 时强制。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ----------------------------------------------------------------------
# 冻结枚举（手册 3.1 / 3.2 / 3.5）
# ----------------------------------------------------------------------


class AgentStatus(enum.StrEnum):
    """运行状态（手册 3.1，11 个值，禁止增删改）。

    终态为 succeeded / failed / cancelled / expired，终态不得再次迁移。
    """

    CREATED = "created"
    PLANNING = "planning"
    WAITING_POLICY = "waiting_policy"
    WAITING_APPROVAL = "waiting_approval"
    EXECUTING = "executing"
    OBSERVING = "observing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class AgentStepType(enum.StrEnum):
    """步骤类型（手册 3.2，8 个值，禁止增删改）。

    每次重规划必须新增 replan 与后续 plan 步骤，不得覆盖历史步骤。
    """

    PLAN = "plan"
    POLICY = "policy"
    APPROVAL_REQUEST = "approval_request"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    VERIFICATION = "verification"
    REPLAN = "replan"
    TERMINAL = "terminal"


class RiskLevel(enum.StrEnum):
    """工具风险级别（手册 3.5，4 个值，禁止增删改）。"""

    READ_ONLY = "read_only"
    WRITE = "write"
    DEVICE_COMMAND = "device_command"
    SENSITIVE = "sensitive"


# 终态集合（状态字符串），终态不得再次迁移
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {s.value for s in (AgentStatus.SUCCEEDED, AgentStatus.FAILED, AgentStatus.CANCELLED, AgentStatus.EXPIRED)}
)

# 合法状态迁移表（确定性内核的状态机，见 runtime.py::_transition）
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    AgentStatus.CREATED.value: {AgentStatus.PLANNING.value},
    AgentStatus.PLANNING.value: {AgentStatus.WAITING_POLICY.value, AgentStatus.EXECUTING.value},
    AgentStatus.WAITING_POLICY.value: {
        AgentStatus.WAITING_APPROVAL.value,
        AgentStatus.EXECUTING.value,
    },
    AgentStatus.WAITING_APPROVAL.value: {AgentStatus.EXECUTING.value},
    AgentStatus.EXECUTING.value: {AgentStatus.OBSERVING.value, AgentStatus.VERIFYING.value},
    AgentStatus.OBSERVING.value: {AgentStatus.EXECUTING.value, AgentStatus.VERIFYING.value},
    AgentStatus.VERIFYING.value: {AgentStatus.PLANNING.value},  # 观察→验证→重规划→安全终止
}


# ----------------------------------------------------------------------
# 请求 / 运行 / 步骤 / 结果
# ----------------------------------------------------------------------


@dataclass
class AgentRunRequest:
    """触发一次 Agent 运行的请求。

    - trigger_type：事件/人工/定时触发来源标识
    - objective：本次运行目标（业务目标描述）
    - actor / role：发起人与其角色（策略守卫按角色放行工具）
    - params：业务参数，供规则计划绑定（如 {"event_id": "evt_001"}）
    - idempotency_key：重复触发幂等键（同一键复用已有 run，不新建）
    - plan_key：选择哪一套规则计划（对应 RuntimeConfig.rule_plans 的键）。
      None 表示用 RuntimeConfig.rule_plan（即既有单角色派单管道）。
      多角色协同只是「换一套计划」，不改内核状态机，也不改
      任何一步的既有语义。
    - lessons_context：调用方提供的业务上下文，供跨 run 经验按条件检索
      （如 {"merge_candidate": true, "high_priority": true}）。
      ★ 内核不自己从数据库里翻业务状态：调用方知道什么就传什么，
        不知道就不传 —— 这样经验引用永远是基于真实已知事实的，
        而不是内核猜出来的。
    """

    trigger_type: str
    objective: str
    actor: str = "operator"
    role: str = "operator"
    params: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    trace_id: str | None = None
    plan_key: str | None = None
    lessons_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentStep:
    """运行轨迹步骤（字段对齐 t_agent_step）。

    解密预算：只保存决策摘要（decision_summary）与输入/输出哈希
    （input_hash / output_hash），绝不保存模型私有思维链或原始载荷。
    """

    step_id: str
    run_id: str
    step_no: int
    step_type: str  # AgentStepType 的值
    decision_summary: str
    tool_name: str | None = None
    tool_version: str | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    status: str = "ok"  # ok / failed / pending
    latency_ms: int = 0
    error_code: str | None = None
    created_at: datetime | None = None


@dataclass
class AgentRun:
    """一次运行的聚合根（字段对齐 t_agent_run）。

    `request` 与 `runtime_state` 是内存实现的续跑上下文：
    审批挂起后 resume() 需要知道「计划执行到哪、绑定变量是什么」。
    WP-02 落 ORM 时这两块应序列化为 run_state JSON 字段（见风险清单）。
    """

    run_id: str
    trigger_type: str
    objective: str
    status: str  # AgentStatus 的值
    request: AgentRunRequest = field(repr=False)
    policy_version: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    termination_reason: str | None = None
    trace_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    runtime_state: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class AgentRunResult:
    """run / cancel / resume 的返回结果。"""

    run_id: str
    status: str  # AgentStatus 的值
    termination_reason: str | None = None
    error_code: str | None = None
    steps: list[AgentStep] = field(default_factory=list)
    trace_id: str | None = None
    objective: str = ""
    trigger_type: str = ""
    policy_version: str | None = None
    finished_at: datetime | None = None
    pending_approval_ids: list[str] = field(default_factory=list)
    idempotent_replay: bool = False


@dataclass
class RuntimeStatus:
    """`AgentRuntime.status()` 的返回（运行期快照）。

    - state：idle（无活跃 run）/ running（存在活跃 run）
    - model_available：大模型是否可用
    - rule_mode：当前是否运行在确定性规则模式
    """

    state: str
    model_available: bool
    rule_mode: bool
    policy_version: str
    active_runs: int
    total_runs: int
    pending_approvals: int
    tools_registered: int
    uptime_ms: int


# ----------------------------------------------------------------------
# 规则计划（确定性规划，无大模型）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Expectation:
    """步骤期望（确定性验证规则，代替模型判断）。

    - key：检查输出 data 的哪个字段
    - min_items：字段为列表/字符串时最小长度
    - contains：字段必须等于的候选值集合（命中任一即通过）
    - on_violation：terminate（安全失败）/ replan（重规划重试）
    - error_code：不满足时的结构化错误码
    """

    key: str
    min_items: int | None = None
    contains: tuple[Any, ...] = ()
    on_violation: str = "terminate"
    error_code: str = "tool_failed"


@dataclass(frozen=True)
class RuleStep:
    """规则计划中的一个步骤。

    - input：支持 "{var}" 占位符，由已完成的工具输出与请求参数绑定
    - on_error：工具执行失败（tool_timeout / tool_failed）时 terminate / replan
    - role：产出该步骤的智能体角色（多角色协同模式用，单角色模式为 None）。
      ★ 这是内核内的计划元数据，**不落 t_agent_step**：轨迹的角色归属由
      `AgentRun.runtime_state["step_roles"]`（step_no → 角色）承载，
      和 plan/bindings 一样随 run 状态快照走。这样既能让轨迹与前端显示
      「这一步是谁做的」，又不改动手册 3.7 冻结的步骤表结构。
    """

    tool: str
    input: dict[str, Any]
    summary: str
    expect: Expectation | None = None
    on_error: str = "terminate"
    role: str | None = None


# ----------------------------------------------------------------------
# 运行时配置（可注入，规则模式的全部可调参数）
# ----------------------------------------------------------------------


@dataclass
class RuntimeConfig:
    """运行时配置（手册 3.6 可注入依赖之一）。

    `model_available=False` 表示模型不可用 —— 内核自动运行在
    确定性规则模式，核心派单闭环不降级。
    """

    policy_version: str = "rules-v1.0"
    max_steps: int = 30          # 步骤轨迹上限（安全终止，防无限循环）
    max_replans: int = 3         # 重规划上限（观察→验证→重规划→安全终止）
    tool_timeout_ms: int = 5000  # 工具超时（毫秒）
    retry_limit: int = 1         # 工具失败/超时后的重试上限
    approval_timeout_ms: int = 300_000  # 审批超时（毫秒），超时 → approval_timeout
    run_ttl_ms: int = 86_400_000        # 运行总时长上限，超时 → run_expired
    model_available: bool = False       # 模型不可用 → 规则模式
    rule_plan: list[dict[str, Any]] | None = None  # None → 内置派单管道
    # 可选计划注册表：request.plan_key 命中时用它，未命中回落 rule_plan。
    # 多角色协同（事件研判 Agent → 调度执行 Agent）即由此挂载，
    # 保证单角色默认路径逐字节不变。
    rule_plans: dict[str, list[dict[str, Any]]] | None = None
    # 跨 run 经验闭环（lessons）：开启后规划阶段会检索同类事件的历史经验，
    # 命中则写进 plan 步骤摘要；run 到终态时按确定性规则复盘提炼新经验。
    # 默认关闭 —— 评测基线场景自带 RuntimeConfig，行为必须逐字节不变。
    lessons_enabled: bool = False
    require_approval_risk: tuple[RiskLevel, ...] = (
        RiskLevel.DEVICE_COMMAND,
        RiskLevel.SENSITIVE,
    )
