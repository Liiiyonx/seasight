"""chat assistant: 新增对话助手两表 t_chat_session / t_chat_message

Revision ID: 20260922_1000_chat_session
Revises: 20260919_1900_knowledge_assets
Create Date: 2026-09-22 10:00:00

★ 线性升级链（保持单一 head）：
   ... → 20260919_1800_approver_role → 20260919_1900_knowledge_assets →
   **本迁移（20260922_1000_chat_session）**

★ revision id 取 26 字符（20260922_1000_chat_session），不超过
   alembic_version.version_num VARCHAR(32) 上限。

★ 双轨制（与 20260919_1700_task_ack 一致）：
   · 新环境：01_schema.sql 已（幂等）建好两张表，初始化走
     `alembic stamp head`，本迁移不执行、不会重复建表；
   · 存量环境：`alembic upgrade head` —— 本迁移建表。
   · downgrade 只 drop 本迁移创建的对象（索引 → 表，按依赖序：
     先 message 后 session，因为 message 外键引用 session），
     不删除任何既有业务表。

★ 本模块 import app.models.chat：把两张表注册到 Base.metadata，
   使 alembic autogenerate 能看到它们（alembic/env.py 共享文件同时注册，
   双保险：迁移自身可独立导入验证）。

★ 字段/约束/索引命名与 app/models/chat.py 及 01_schema.sql 完全一致，
   避免 Alembic autogenerate 漂移。role 用 CHECK 约束而非 PG 枚举，
   避免枚举三处同步。payload 用 JSONB（迁移只跑 PostgreSQL，
   SQLite 变体仅在 ORM 层为内存测试声明）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 导入 ORM 模型，使两张表注册到 Base.metadata（见模块 docstring）。
import app.models.chat  # noqa: F401

# revision identifiers, used by Alembic.
# ★ 不得超过 32 字符（alembic_version.version_num 为 VARCHAR(32)）。
revision: str = "20260922_1000_chat_session"
down_revision: str | None = "20260919_1900_knowledge_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 t_chat_session 与 t_chat_message（含约束与索引）。"""
    op.create_table(
        "t_chat_session",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", name="uq_chat_session_session_id"),
    )
    op.create_index("idx_chat_session_username", "t_chat_session", ["username"])
    op.create_index(
        "idx_chat_session_updated", "t_chat_session",
        [sa.text("updated_at DESC")],
    )

    op.create_table(
        "t_chat_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "content_type", sa.String(length=32),
            nullable=False, server_default=sa.text("'text'"),
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_chat_message_message_id"),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')", name="chk_chat_message_role",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["t_chat_session.session_id"],
            name="fk_chat_message_session", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_chat_message_session", "t_chat_message", ["session_id", "id"],
    )


def downgrade() -> None:
    """回退本迁移：只删除本迁移创建的对象，按依赖序（先 message 后 session）。"""
    op.drop_index("idx_chat_message_session", table_name="t_chat_message")
    op.drop_table("t_chat_message")
    op.drop_index("idx_chat_session_updated", table_name="t_chat_session")
    op.drop_index("idx_chat_session_username", table_name="t_chat_session")
    op.drop_table("t_chat_session")
