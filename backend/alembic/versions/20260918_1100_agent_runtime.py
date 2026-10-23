"""agent runtime: 新增 t_agent_run / t_agent_step / t_agent_memory / t_agent_approval

Revision ID: 20260918_1100_agent_runtime
Revises: 20260918_1000_baseline
Create Date: 2026-09-18 11:00:00

★ 双轨制说明（与 baseline 一致）：
   · 新环境：01_schema.sql 已（幂等）建好四张 agent 表 + 枚举类型，
     初始化走 `alembic stamp head`，本迁移不执行、不会重复建表。
   · 存量环境（如本机开发库）：业务表已存在、agent 表不存在，
     直接 `alembic upgrade head` —— baseline 为空迁移，本迁移负责建表。
   · 枚举类型用 DO/duplicate_object 守卫幂等创建（与 01_schema.sql 同写法），
     因此本迁移在「类型已存在」的库上也能安全执行。
   · downgrade 只 drop 本迁移创建的四张 agent 表（按依赖序），
     不删除任何既有业务表；枚举类型刻意保留（由 01_schema.sql 幂等管理）。

★ 字段/约束/索引命名与 backend/app/models/agent.py 及
   backend/db/init/01_schema.sql 完全一致，避免 autogenerate 漂移。

★ 本模块 import app.models.agent：把四张 agent 表注册到 Base.metadata，
   使 alembic autogenerate 在 alembic/env.py 补 `import app.models.agent`
   之前也能看到这些表（env.py 的显式 import 仍建议由总控补上）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 导入 ORM 模型，使 t_agent_* 注册到 Base.metadata（见模块 docstring）。
import app.models.agent  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "20260918_1100_agent_runtime"
down_revision: str | None = "20260918_1000_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 四张表与其唯一约束/关联 —— 测试用 AST 对账的锚点。
AGENT_TABLES = ("t_agent_run", "t_agent_step", "t_agent_memory", "t_agent_approval")

# 枚举类型（值语义对齐手册 3.1/3.2/3.3/3.5 与 01_schema.sql）。
# ★ 步骤状态/记忆作用域/记忆来源为自由 VARCHAR（与 WP-01 内存模型对齐），
#   不设数据库枚举；审批决策对齐 WP-01：approved / rejected / cancelled。
AGENT_ENUMS: dict[str, tuple[str, ...]] = {
    "agent_run_status_enum": (
        "created", "planning", "waiting_policy", "waiting_approval",
        "executing", "observing", "verifying",
        "succeeded", "failed", "cancelled", "expired",
    ),
    "agent_step_type_enum": (
        "plan", "policy", "approval_request", "tool_call",
        "observation", "verification", "replan", "terminal",
    ),
    "agent_error_code_enum": (
        "no_robot_available", "tool_timeout", "tool_failed",
        "policy_denied", "approval_rejected", "approval_timeout",
        "task_conflict", "invalid_tool_input", "invalid_tool_output",
        "max_steps_exceeded", "run_expired", "internal_error",
    ),
    "agent_memory_type_enum": ("working", "episodic", "semantic", "policy", "eval"),
    "agent_risk_level_enum": ("read_only", "write", "device_command", "sensitive"),
    "agent_decision_enum": ("approved", "rejected", "cancelled"),
}


def _create_enum_types() -> None:
    """幂等创建 agent 枚举类型（与 01_schema.sql 同写法）。"""
    for name, values in AGENT_ENUMS.items():
        values_sql = ", ".join(f"'{v}'" for v in values)
        op.execute(
            f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({values_sql}); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
        )


def upgrade() -> None:
    """创建 agent 四表（含唯一约束与外键）。"""
    _create_enum_types()

    op.create_table(
        "t_agent_run",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="agent_run_status_enum", create_type=False),
            nullable=False,
            server_default=sa.text("'created'"),
        ),
        sa.Column("policy_version", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("termination_reason", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_agent_run_run_id"),
    )
    op.create_index("idx_agent_run_status", "t_agent_run", ["status"])
    op.create_index(
        "idx_agent_run_created", "t_agent_run",
        [sa.text("created_at DESC")],
    )

    op.create_table(
        "t_agent_step",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("step_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column(
            "step_type",
            postgresql.ENUM(name="agent_step_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("decision_summary", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column("tool_version", sa.String(length=32), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "status", sa.String(length=16),
            nullable=False, server_default=sa.text("'pending'"),
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "error_code",
            postgresql.ENUM(name="agent_error_code_enum", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("step_id", name="uq_agent_step_step_id"),
        sa.UniqueConstraint("run_id", "step_no", name="uq_agent_step_run_no"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["t_agent_run.run_id"],
            name="fk_agent_step_run", ondelete="CASCADE",
        ),
    )

    op.create_table(
        "t_agent_memory",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column(
            "memory_type",
            postgresql.ENUM(name="agent_memory_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=True),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("memory_id", name="uq_agent_memory_memory_id"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="chk_agent_memory_confidence",
        ),
    )
    op.create_index(
        "idx_agent_memory_scope", "t_agent_memory", ["scope_type", "scope_id"],
    )

    op.create_table(
        "t_agent_approval",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("approval_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("requested_action", sa.Text(), nullable=False),
        sa.Column(
            "risk_level",
            postgresql.ENUM(name="agent_risk_level_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("requested_by", sa.String(length=64), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column(
            "decision",
            postgresql.ENUM(name="agent_decision_enum", create_type=False),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_id", name="uq_agent_approval_approval_id"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["t_agent_run.run_id"],
            name="fk_agent_approval_run", ondelete="CASCADE",
        ),
    )
    op.create_index("idx_agent_approval_run", "t_agent_approval", ["run_id"])
    op.create_index("idx_agent_approval_decision", "t_agent_approval", ["decision"])
    op.create_index(
        "idx_agent_approval_requested", "t_agent_approval",
        [sa.text("requested_at DESC")],
    )


def downgrade() -> None:
    """回退本迁移：只删除本迁移创建的四张 agent 表。

    ★ 按依赖序删除（approval / memory / step 先于 run）；
      不删除任何既有业务表；枚举类型刻意保留 ——
      它们由 01_schema.sql 幂等创建，删除类型对回退无必要且有副作用。
    """
    op.drop_table("t_agent_approval")
    op.drop_table("t_agent_memory")
    op.drop_table("t_agent_step")
    op.drop_table("t_agent_run")
