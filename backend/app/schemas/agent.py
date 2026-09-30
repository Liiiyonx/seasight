"""Agent 运行 API 契约。

这些 Schema 只描述 HTTP 边界，不复制 Agent 内核状态机。真正的状态值、
工具风险级别和错误摘要仍以 `app.services.agents` 为唯一真源。

分页结构复用 `app.schemas.PageMeta`（与其它列表接口的 PageResult 风格一致）：
    GET /api/v1/agents/runs -> ApiResponse[AgentRunPageOut]
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import PageMeta


class AgentRunCreate(BaseModel):
    """启动一次事件处置运行。"""

    event_id: str = Field(..., min_length=1, max_length=64, description="待处置事件编号")
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="调用方幂等键；重复提交时复用已有运行（返回同一 run_id，标记 replay）",
    )
    objective: str | None = Field(
        default=None,
        max_length=500,
        description="业务目标；为空时使用系统默认的确定性命中目标",
    )
    # 计划模式：
    #   single / None → 既有单角色派单管道（默认，行为与历史版本完全一致）
    #   team          → 事件研判 Agent（政策检索 + 研判门禁）→ 调度执行 Agent
    # 取值必须与 app.api.v1.agents.RULE_PLANS 的键一致；运行时会拒绝未知的
    # plan_key，而不是静默回落到单角色（静默回落会让"选了多角色却跑出单人
    # 轨迹"这种失真被当成正常结果展示出去）。
    mode: Literal["single", "team"] | None = Field(
        default=None,
        description="计划模式：single=单角色派单管道（默认）；team=多角色协同",
    )


class AgentRunCancel(BaseModel):
    """取消一次运行。"""

    reason: str = Field(default="操作员取消", min_length=1, max_length=300)


class AgentApprovalDecision(BaseModel):
    """人工审批决定。"""

    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=500)


class AgentStepOut(BaseModel):
    """运行轨迹中的单步。"""

    model_config = ConfigDict(from_attributes=True)

    step_id: str
    run_id: str
    step_no: int
    step_type: str
    decision_summary: str
    tool_name: str | None = None
    tool_version: str | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    status: str
    latency_ms: int = 0
    error_code: str | None = None
    created_at: datetime | None = None
    # 产出该步骤的智能体角色（多角色协同计划才有值）。
    # 真源是 AgentRun.runtime_state["step_roles"]，不占 t_agent_step 列。
    role: str | None = None


class AgentDecisionOut(BaseModel):
    """派单决策快照（供大屏地图联动与答辩复盘）。

    真源是 run 状态里的绑定变量（device.query_available / dispatch.plan /
    analysis.assess 三步的工具输出），不是另算一份 —— 屏幕上标注的候选与理由，
    和轨迹里那几步的实际输出必然一致。
    """

    candidates: list[dict[str, Any]] = Field(default_factory=list)
    selected_robot_id: str | None = None
    selected_robot_name: str | None = None
    distance_m: float | None = None
    reason: str | None = None
    risk_level: str | None = None
    recommended_action: str | None = None
    basis: list[str] = Field(default_factory=list)


class AgentRunOut(BaseModel):
    """运行摘要与可选轨迹。"""

    run_id: str
    trigger_type: str
    objective: str
    status: str
    policy_version: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    termination_reason: str | None = None
    trace_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_code: str | None = None
    idempotent_replay: bool = False
    pending_approval_ids: list[str] = Field(default_factory=list)
    steps: list[AgentStepOut] = Field(default_factory=list)
    task_id: str | None = None
    task_action: str | None = None
    # 本次运行用的计划变体（single / team）；team 时 roles 按出场顺序列出
    # 参与协同的角色，供前端渲染「谁交给了谁」。
    plan_key: str | None = None
    roles: list[str] = Field(default_factory=list)
    # 派单决策快照（候选机器人 + 选中者 + 理由）；未走到派单步骤时为 null。
    decision: AgentDecisionOut | None = None
    # 本次运行引用到的经验 id（plan 步骤摘要里也有一句人话版本）。
    lesson_hits: list[str] = Field(default_factory=list)


class AgentRunPageOut(BaseModel):
    """运行列表分页结果（PageResult 风格：items + meta）。

    `meta` 与通用 `PageMeta` 同构（total / page / page_size / pages），
    保证前端与其它列表接口（事件、任务、报表）共用一套分页消费逻辑。
    """

    items: list[AgentRunOut] = Field(default_factory=list)
    meta: PageMeta = Field(default_factory=PageMeta)


class AgentLessonOut(BaseModel):
    """一条跨 run 经验（Lessons）。

    真源是内核 `LessonStore`（落在内存记忆仓储上，memory_type=episodic）。
    这里只是只读投影：前端拿它渲染「经验库」与「本次引用了哪条经验」。
    """

    lesson_id: str
    scope_id: str                 # 事件类别
    kind: str                     # LESSON_KINDS 之一
    condition: str                # 适用条件（人话）
    guidance: str                 # 结论 / 动作建议
    confidence: float
    hit_count: int = 0
    confirm_count: int = 0
    source_run_id: str | None = None
    updated_at: datetime | None = None


class AgentRuntimeStatusOut(BaseModel):
    """运行时快照。"""

    state: str
    model_available: bool
    rule_mode: bool
    policy_version: str
    active_runs: int
    total_runs: int
    pending_approvals: int
    tools_registered: int
    uptime_ms: int
    persistence: str = "mirrored"
    runtime: str = "in_process"
    # 生效中的审批策略（真源：RuntimeConfig.require_approval_risk，不是配置猜测）。
    # 演示前用它可以 30 秒自检「审批高光到底开没开」，避免录到一半发现
    # 工单被智能体直接建掉、没有待审批项。
    require_approval_for_write: bool = False
    approval_risk_levels: list[str] = Field(default_factory=list)
    # 运行时支持的计划变体（single / team）：前端据此决定要不要显示
    # 「多角色协同」开关，而不是写死一个可能不被后端支持的选项。
    plan_variants: list[str] = Field(default_factory=list)
    # 跨 run 经验库当前快照（进程内累积；重启清零 —— 见 agents.py 装配注释）。
    # 不可用时为空数组，前端按"经验库为空"渲染，不伪造条数。
    lessons: list[AgentLessonOut] = Field(default_factory=list)


class AgentToolOut(BaseModel):
    """公开工具目录。"""

    name: str
    version: str
    description: str
    risk_level: str
    timeout_ms: int
    idempotent: bool
    allowed_roles: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class AgentApprovalOut(BaseModel):
    """审批记录。"""

    approval_id: str
    run_id: str
    requested_action: str
    risk_level: str
    requested_by: str
    decided_by: str | None = None
    decision: str | None = None
    reason: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None


class AgentEvalExpectedOut(BaseModel):
    """场景期望结果（WP-05 expected 子结构）。"""

    status: str | None = None
    error_code: str | None = None
    shape: str | None = None


class AgentEvalActualOut(BaseModel):
    """场景实际结果（WP-05 actual 子结构）。"""

    status: str | None = None
    error_code: str | None = None
    termination_reason: str | None = None
    step_count: int | None = None
    step_types: list[str] = Field(default_factory=list)
    tool_executions: dict[str, Any] = Field(default_factory=dict)
    decision_latency_ms: float | None = None


class AgentEvalScenarioOut(BaseModel):
    """单个固定评测场景。"""

    id: str
    name: str | None = None
    description: str | None = None
    evidence_level: str | None = None
    passed: bool
    skipped: bool = False
    skip_reason: str | None = None
    expected: AgentEvalExpectedOut | None = None
    actual: AgentEvalActualOut | None = None
    notes: list[Any] = Field(default_factory=list)


class AgentEvalMetricsOut(BaseModel):
    """聚合指标：WP-05 六项 + WP-12 七项 + 口径文档 + 分母台账。"""

    success_rate: float | None = None
    policy_violation_rate: float | None = None
    tool_correct_rate: float | None = None
    invalid_loop_rate: float | None = None
    recovery_success_rate: float | None = None
    p95_decision_latency_ms: float | None = None
    restart_recovery_rate: float | None = None
    idempotency_conflict_rate: float | None = None
    model_fallback_rate: float | None = None
    model_schema_rejection_rate: float | None = None
    trace_replay_match_rate: float | None = None
    approval_handoff_success_rate: float | None = None
    device_fault_recovery_rate: float | None = None
    business_success_rate: float | None = None
    definitions: dict[str, str] = Field(default_factory=dict)
    denominators: dict[str, int] = Field(default_factory=dict)


class AgentEvalOut(BaseModel):
    """最新固定场景评测结果 —— 映射 WP-05/WP-12 产物
    `artifacts/agent_evals/latest_v2.json`
    的真实契约。

    契约真源：`backend/tests/agent_evals/contract.py`（v2 冻结清单，
    本文件常量由防漂移测试与之一一对账）。端点先做严格结构校验，
    不合法返回 6008；本 schema 只承载合法产物。已移除旧版
    total/passed/failed/cases 占位字段 —— 杜绝「静默全零」。
    """

    available: bool = True
    report_type: str
    schema_version: str
    evidence_level: str
    command: str | None = None
    date: datetime | str | None = None
    code_version: dict[str, Any] = Field(default_factory=dict)   # source/value/fingerprint/note
    config: dict[str, Any] = Field(default_factory=dict)         # hash/scenario_count/scenario_ids/files/kernel_files
    environment: dict[str, Any] = Field(default_factory=dict)    # network/model/device_access + backend
    sample_size: int
    skipped_count: int = 0
    scenarios: list[AgentEvalScenarioOut] = Field(default_factory=list)
    metrics: AgentEvalMetricsOut = Field(default_factory=AgentEvalMetricsOut)
    source: str
