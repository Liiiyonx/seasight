"""task ack: 新增 ACK 审计账本 t_task_ack（WP-14D）

Revision ID: 20260919_1700_task_ack
Revises: 20260919_1600_agent_state
Create Date: 2026-09-19 17:00:00

★ 线性升级链（保持单一 head）：
   20260918_1000_baseline → 20260918_1100_agent_runtime →
   20260919_1200_coverage_nullable → 20260919_1300_clear_coverage →
   20260919_1600_agent_state → **本迁移（20260919_1700_task_ack）**

★ revision id 取 21 字符（20260919_1700_task_ack），不超过
   alembic_version.version_num VARCHAR(32) 上限。

★ 双轨制（与 20260919_1600_agent_persistent_state 一致）：
   · 新环境：01_schema.sql 已（幂等）建好 t_task_ack，初始化走
     `alembic stamp head`，本迁移不执行、不会重复建表；
   · 存量环境（如本机开发库）：`alembic upgrade head` —— 本迁移建表。
   · downgrade 只 drop 本迁移创建的对象（索引 → 表，按依赖序），
     不删除任何既有业务表。

★ 本模块 import app.models.task_ack：把 t_task_ack 注册到 Base.metadata，
   使 alembic autogenerate 能看到该表（alembic/env.py 共享文件同时注册，
   双保险：迁移自身可独立导入验证）。

★ 字段/约束/索引命名与 app/models/task_ack.py 及 01_schema.sql 完全一致，
   避免 Alembic autogenerate 漂移。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 导入 ORM 模型，使 t_task_ack 注册到 Base.metadata（见模块 docstring）。
import app.models.task_ack  # noqa: F401

# revision identifiers, used by Alembic.
# ★ 不得超过 32 字符（alembic_version.version_num 为 VARCHAR(32)）。
revision: str = "20260919_1700_task_ack"
down_revision: str | None = "20260919_1600_agent_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 t_task_ack（含 command_id 唯一约束与三条冻结索引）。"""
    op.create_table(
        "t_task_ack",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "received_wall_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "duplicate_count", sa.Integer(),
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column("last_duplicate_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("last_payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id", name="uq_task_ack_command_id"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["t_task.task_id"],
            name="fk_task_ack_task", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_task_ack_task", "t_task_ack", ["task_id", sa.text("received_wall_at DESC")],
    )
    op.create_index(
        "idx_task_ack_device_seq", "t_task_ack", ["device_id", sa.text("seq DESC")],
    )
    op.create_index(
        "idx_task_ack_outcome", "t_task_ack", ["outcome", sa.text("received_wall_at DESC")],
    )


def downgrade() -> None:
    """回退本迁移：只删除本迁移创建的对象，不删除任何既有业务表。"""
    op.drop_index("idx_task_ack_outcome", table_name="t_task_ack")
    op.drop_index("idx_task_ack_device_seq", table_name="t_task_ack")
    op.drop_index("idx_task_ack_task", table_name="t_task_ack")
    op.drop_table("t_task_ack")
