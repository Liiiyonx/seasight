"""agent persistent state: 新增 t_agent_run_state（WP-10 持久化续跑状态）

Revision ID: 20260919_1600_agent_state
Revises: 20260919_1300_clear_coverage
Create Date: 2026-09-19 16:00:00

★ down_revision 为什么是 1300 而不是任务消息里写的 1200（重要偏差）：
   任务消息基于「1200 是迁移链 head」的旧前提，只列了三份迁移文件；
   但 backend/alembic/versions/ 下实际还存在 20260919_1300_clear_coverage.py
   （WP-07 数据清理，`alembic heads` 实测报告其为唯一 head）。
   若仍接 1200，迁移图会出现两个 head，真库 `alembic upgrade head`
   会直接报 "Multiple head revisions are present" —— 违反全局硬约束
   「不得破坏既有」与「迁移必须可升级」。因此按真值接 1300，
   保持线性链：1000 → 1100 → 1200 → 1300 → 本迁移。

★ 文件名与 revision id 不同（任务要求文件名为
   20260919_1600_agent_persistent_state.py，保持原样）：
   revision id 受 alembic_version.version_num VARCHAR(32) 约束，
   该文件名去掉前缀后仍超 32 字符（36），会在真库升级时报
   StringDataRightTruncation，故 revision 取 25 字符的
   20260919_1600_agent_state。

★ 双轨制说明（与 20260918_1100_agent_runtime 一致）：
   · 新环境：01_schema.sql 已（幂等）建好 t_agent_run_state，
     初始化走 `alembic stamp head`，本迁移不执行、不会重复建表。
   · 存量环境（如本机开发库）：agent 表已存在、t_agent_run_state 不存在，
     直接 `alembic upgrade head` —— 本迁移负责建表。
   · downgrade 只 drop 本迁移创建的对象（索引 → 表，按依赖序），
     不删除任何既有业务表；不触碰枚举类型。

★ 本模块 import app.models.agent_state：把 t_agent_run_state 注册到
   Base.metadata，使 alembic autogenerate 能看到该表
   （alembic/env.py 是共享文件、不得修改，因此不在那里注册）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 导入 ORM 模型，使 t_agent_run_state 注册到 Base.metadata（见模块 docstring）。
import app.models.agent_state  # noqa: F401

# revision identifiers, used by Alembic.
# ★ 不得超过 32 字符（alembic_version.version_num 为 VARCHAR(32)）。
revision: str = "20260919_1600_agent_state"
down_revision: str | None = "20260919_1300_clear_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 t_agent_run_state（含幂等键唯一部分索引与版本检索索引）。"""
    op.create_table(
        "t_agent_run_state",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("runtime_state_json", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "state_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_agent_run_state_run_id"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["t_agent_run.run_id"],
            name="fk_agent_run_state_run", ondelete="CASCADE",
        ),
    )
    # 幂等键唯一部分索引：NULL 不参与唯一（同一幂等键并发创建最多一个 run）。
    op.create_index(
        "uq_agent_run_state_idem_key",
        "t_agent_run_state",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    # (run_id, state_version) 乐观并发检查的检索索引。
    op.create_index(
        "idx_agent_run_state_version",
        "t_agent_run_state",
        ["run_id", "state_version"],
    )


def downgrade() -> None:
    """回退本迁移：只删除本迁移创建的对象，不删除任何既有业务表。"""
    op.drop_index("idx_agent_run_state_version", table_name="t_agent_run_state")
    op.drop_index("uq_agent_run_state_idem_key", table_name="t_agent_run_state")
    op.drop_table("t_agent_run_state")
