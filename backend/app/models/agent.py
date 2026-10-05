"""Agent 运行契约 ORM 模型 —— Run / Step / Memory / Approval 四表。

表结构与《Harness 分工执行手册》3.7 节冻结契约逐字段对齐：
    t_agent_run      (id, run_id, trigger_type, objective, status, policy_version,
                      started_at, finished_at, termination_reason, trace_id,
                      created_at, updated_at)
    t_agent_step     (id, step_id, run_id, step_no, step_type, decision_summary,
                      tool_name, tool_version, input_hash, output_hash,
                      status, latency_ms, error_code, created_at)
    t_agent_memory   (id, memory_id, memory_type, scope_type, scope_id,
                      content, confidence, source_type, source_id,
                      valid_from, valid_to, created_at)
    t_agent_approval (id, approval_id, run_id, requested_action, risk_level,
                      requested_by, decided_by, decision, reason,
                      requested_at, decided_at)

枚举语义对齐：
    运行状态   → 手册 3.1（11 个值，终态 4 个，终态不得再迁移）
    步骤类型   → 手册 3.2（8 个值）
    错误码     → 手册 3.3（12 个值）
    风险级别   → 手册 3.5（read_only / write / device_command / sensitive）

★ 设计纪律：不保存模型私有思维链，只保存决策摘要、工具输入/输出摘要
  与业务理由（input_hash / output_hash 只存摘要的哈希，不存原始内容）。

字段/约束/索引命名与 backend/db/init/01_schema.sql 及
alembic 增量迁移 20260918_1100_agent_runtime 完全一致，
保证 Alembic autogenerate 对这三处不会产生漂移差异。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AgentRunStatus:
    """运行状态（手册 3.1，值冻结）。

    TERMINAL = succeeded / failed / cancelled / expired，
    终态不得再次迁移；每次重规划新增 replan 与后续步骤，不覆盖历史。
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

    ALL = (
        CREATED, PLANNING, WAITING_POLICY, WAITING_APPROVAL,
        EXECUTING, OBSERVING, VERIFYING,
        SUCCEEDED, FAILED, CANCELLED, EXPIRED,
    )
    TERMINAL = (SUCCEEDED, FAILED, CANCELLED, EXPIRED)


class AgentStepType:
    """步骤类型（手册 3.2，值冻结）。"""

    PLAN = "plan"                      # 生成计划
    POLICY = "policy"                  # 策略守卫判断
    APPROVAL_REQUEST = "approval_request"  # 请求人工审批
    TOOL_CALL = "tool_call"            # 工具调用
    OBSERVATION = "observation"        # 观察工具结果/环境
    VERIFICATION = "verification"      # 验证结果
    REPLAN = "replan"                  # 重规划（不得覆盖历史步骤）
    TERMINAL = "terminal"              # 终止步骤

    ALL = (
        PLAN, POLICY, APPROVAL_REQUEST, TOOL_CALL,
        OBSERVATION, VERIFICATION, REPLAN, TERMINAL,
    )


class AgentStepStatus:
    """步骤状态（手册未冻结，**推荐取值**，与 WP-01 内核实际写入值对齐）。

    ★ 本列在库中为 VARCHAR(16) 自由取值（兼容 WP-01 内存模型）：
      · 常规步骤：WP-01 写入 ok / failed（pending 为库默认值）
      · 终态步骤：写入 run 的状态值 succeeded / failed / cancelled / expired
    TERMINAL 仅为文档性推荐，不做数据库约束。
    """

    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    ALL = (PENDING, OK, FAILED, SUCCEEDED, CANCELLED, EXPIRED)
    TERMINAL = (SUCCEEDED, FAILED, CANCELLED, EXPIRED)


class AgentErrorCode:
    """结构化错误码（手册 3.3，值冻结）。"""

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

    ALL = (
        NO_ROBOT_AVAILABLE, TOOL_TIMEOUT, TOOL_FAILED, POLICY_DENIED,
        APPROVAL_REJECTED, APPROVAL_TIMEOUT, TASK_CONFLICT,
        INVALID_TOOL_INPUT, INVALID_TOOL_OUTPUT, MAX_STEPS_EXCEEDED,
        RUN_EXPIRED, INTERNAL_ERROR,
    )


class AgentRiskLevel:
    """工具/审批风险级别（手册 3.5，值冻结）。"""

    READ_ONLY = "read_only"
    WRITE = "write"
    DEVICE_COMMAND = "device_command"
    SENSITIVE = "sensitive"

    ALL = (READ_ONLY, WRITE, DEVICE_COMMAND, SENSITIVE)


class AgentDecision:
    """审批决定（与 WP-01 ApprovalRecord.decision 对齐，库内为 PG 枚举）。

    approved = 同意；rejected = 拒绝（run 终止，不发送设备指令）；
    cancelled = 随 run 取消。审批超时不留决策值：record.decision 保持
    NULL，run 以 approval_timeout 错误码终止。
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

    ALL = (APPROVED, REJECTED, CANCELLED)


class AgentMemoryType:
    """记忆类型（项目计划书 5.7 分层记忆）。"""

    WORKING = "working"        # 工作记忆：当前 run 的计划、步骤、观察
    EPISODIC = "episodic"      # 情景记忆：历史类似事件与处理结果
    SEMANTIC = "semantic"      # 语义记忆：点位、潮汐、垃圾类别、设备特征
    POLICY = "policy"          # 策略记忆：规则、阈值、人工纠正（审批后生效）
    EVAL = "eval"              # 评测记忆：固定场景、期望轨迹、失败样本

    ALL = (WORKING, EPISODIC, SEMANTIC, POLICY, EVAL)


class AgentScopeType:
    """记忆作用域（**推荐取值**，库内为 VARCHAR(32) 自由取值）。

    WP-01 内存模型注释允许 run / robot / township / ... 等任意作用域
    标识（如按设备、按乡镇检索记忆），因此数据库不设枚举约束。
    """

    RUN = "run"                # 作用域为一次 run（scope_id = run_id）
    STEP = "step"              # 作用域为单个 step（scope_id = step_id）
    GLOBAL = "global"          # 全局记忆（scope_id = NULL）

    ALL = (RUN, STEP, GLOBAL)


class AgentSourceType:
    """记忆来源（**推荐取值**，库内为 VARCHAR(32) 自由取值，可空）。

    与 WP-01 内存模型 source_type 对齐：tool_call / policy /
    verification / replan / manual；也允许 observation / model /
    human_review 等。用于区分「事实 / 推断 / 人工确认」——
    禁止把模型猜测写成事实（由应用层强制，见 WP-01 memory.py）。
    """

    MANUAL = "manual"
    TOOL_CALL = "tool_call"
    POLICY = "policy"
    VERIFICATION = "verification"
    REPLAN = "replan"
    OBSERVATION = "observation"
    MODEL = "model"
    HUMAN_REVIEW = "human_review"

    ALL = (MANUAL, TOOL_CALL, POLICY, VERIFICATION, REPLAN, OBSERVATION, MODEL, HUMAN_REVIEW)


class AgentRun(Base):
    """运行记录表 t_agent_run。

    run_id 唯一；status 状态机见 AgentRunStatus；终态后不得再迁移。
    """

    __tablename__ = "t_agent_run"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)          # 业务编号 run_xxx
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)    # event / manual / scheduled ...
    objective: Mapped[str] = mapped_column(Text, nullable=False)             # 目标（决策摘要，无私有思维链）
    status: Mapped[str] = mapped_column(
        SAEnum(*AgentRunStatus.ALL, name="agent_run_status_enum", create_type=False),
        nullable=False,
        server_default=text("'created'"),
    )
    policy_version: Mapped[str | None] = mapped_column(String(32))           # 规则/策略版本（规则模式可为空）
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    termination_reason: Mapped[str | None] = mapped_column(Text)             # 终止原因（业务摘要/结构化错误说明）
    trace_id: Mapped[str | None] = mapped_column(String(64))                 # 链路追踪 ID
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_agent_run_run_id"),
        Index("idx_agent_run_status", "status"),
        Index("idx_agent_run_created", text("created_at DESC")),
    )

    def __repr__(self) -> str:
        return f"<AgentRun {self.run_id} [{self.status}] trigger={self.trigger_type}>"


class AgentStep(Base):
    """步骤记录表 t_agent_step。

    (run_id, step_no) 唯一：重规划必须新增步骤，不覆盖历史；
    run_id 关联 t_agent_run.run_id（不是主键 id）。
    """

    __tablename__ = "t_agent_step"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)         # 业务编号 stp_xxx
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("t_agent_run.run_id", ondelete="CASCADE", name="fk_agent_step_run"),
        nullable=False,
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(
        SAEnum(*AgentStepType.ALL, name="agent_step_type_enum", create_type=False),
        nullable=False,
    )
    decision_summary: Mapped[str] = mapped_column(Text, nullable=False)      # 决策摘要（无私有思维链）
    tool_name: Mapped[str | None] = mapped_column(String(128))               # 仅 tool_call 步骤有值
    tool_version: Mapped[str | None] = mapped_column(String(32))
    input_hash: Mapped[str | None] = mapped_column(String(64))               # 工具输入摘要哈希
    output_hash: Mapped[str | None] = mapped_column(String(64))              # 工具输出摘要哈希
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(
        SAEnum(*AgentErrorCode.ALL, name="agent_error_code_enum", create_type=False)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("step_id", name="uq_agent_step_step_id"),
        UniqueConstraint("run_id", "step_no", name="uq_agent_step_run_no"),
    )

    def __repr__(self) -> str:
        return f"<AgentStep {self.step_id} run={self.run_id} no={self.step_no} [{self.step_type}]>"


class AgentMemory(Base):
    """记忆表 t_agent_memory。

    分层记忆（工作/情景/语义/策略/评测），按 (scope_type, scope_id) 检索；
    content 只存事实/摘要，不存模型私有思维链；
    confidence 为 0~1，与 source_type 配合区分「事实 / 推断 / 人工确认」。
    """

    __tablename__ = "t_agent_memory"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    memory_id: Mapped[str] = mapped_column(String(64), nullable=False)       # 业务编号 mem_xxx
    memory_type: Mapped[str] = mapped_column(
        SAEnum(*AgentMemoryType.ALL, name="agent_memory_type_enum", create_type=False),
        nullable=False,
    )
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)       # run/robot/township/... 自由取值
    scope_id: Mapped[str | None] = mapped_column(String(64))                 # run_id / step_id；global 时为空
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))        # 0.0000 ~ 1.0000
    source_type: Mapped[str | None] = mapped_column(String(32))              # 事实/推断/人工确认来源，可空
    source_id: Mapped[str | None] = mapped_column(String(64))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("memory_id", name="uq_agent_memory_memory_id"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="chk_agent_memory_confidence"),
        Index("idx_agent_memory_scope", "scope_type", "scope_id"),
    )

    def __repr__(self) -> str:
        return f"<AgentMemory {self.memory_id} [{self.memory_type}] scope={self.scope_type}:{self.scope_id}>"


class AgentApproval(Base):
    """审批记录表 t_agent_approval。

    run_id 关联 t_agent_run.run_id；decision 为空 = 待审批；
    requested_action 保存业务动作摘要（如 mqtt.send_task 目标与参数），
    供人工判断，不保存模型私有思维链。
    """

    __tablename__ = "t_agent_approval"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    approval_id: Mapped[str] = mapped_column(String(64), nullable=False)     # 业务编号 apr_xxx
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("t_agent_run.run_id", ondelete="CASCADE", name="fk_agent_approval_run"),
        nullable=False,
    )
    requested_action: Mapped[str] = mapped_column(Text, nullable=False)      # 请求的动作摘要
    risk_level: Mapped[str] = mapped_column(
        SAEnum(*AgentRiskLevel.ALL, name="agent_risk_level_enum", create_type=False),
        nullable=False,
    )
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)    # 提出方（agent 角色/工具）
    decided_by: Mapped[str | None] = mapped_column(String(64))               # 决定人（人工）
    decision: Mapped[str | None] = mapped_column(
        SAEnum(*AgentDecision.ALL, name="agent_decision_enum", create_type=False)
    )
    reason: Mapped[str | None] = mapped_column(Text)                         # 决定理由
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_agent_approval_approval_id"),
        Index("idx_agent_approval_run", "run_id"),
        Index("idx_agent_approval_decision", "decision"),
        Index("idx_agent_approval_requested", text("requested_at DESC")),
    )

    def __repr__(self) -> str:
        return f"<AgentApproval {self.approval_id} run={self.run_id} [{self.risk_level}] decision={self.decision}>"
