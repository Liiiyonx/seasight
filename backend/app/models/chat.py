"""对话助手 ORM 模型 —— 会话 / 消息两表。

本模型独立于「事件处置 Agent」（app.models.agent）：那是派单运行状态机，
这里是自由对话的会话与消息持久化。两者并行，互不干扰。

    t_chat_session (id, session_id, username, title, created_at, updated_at)
    t_chat_message (id, message_id, session_id, role, content, content_type,
                    payload, run_id, created_at)

★ 设计纪律（沿用 app.models.agent）：不保存模型私有思维链。
  payload 只存结构化展示块（检测框 / 事件卡 / 统计卡 / 派单建议卡），
  不存原始 prompt、不存模型思维链；敏感键由服务层脱敏后再入库。

字段/约束/索引命名与 backend/db/init/01_schema.sql 及
alembic 增量迁移完全一致，保证 autogenerate 不漂移。

★ SQLite 兼容（测试）：``BigInteger`` 主键用 ``with_variant(Integer, "sqlite")``
  使内存测试可建表；payload 用 ``JSONB().with_variant(JSON, "sqlite")`` ——
  PostgreSQL 上仍是 JSONB，SQLite 上降级为 JSON（TEXT），保证无 PG 也能跑测试。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ChatRole:
    """对话角色（取值冻结在本模块，用 CHECK 约束而非 PG 枚举，避免枚举三处同步）。"""

    USER = "user"
    ASSISTANT = "assistant"
    ALL = (USER, ASSISTANT)


class ChatSession(Base):
    """会话表 t_chat_session。

    session_id 唯一业务编号；username 为属主（数据隔离：仅属主可见/可删）。
    """

    __tablename__ = "t_chat_session"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)      # 业务编号 cs_xxx
    username: Mapped[str] = mapped_column(String(64), nullable=False)        # 属主
    title: Mapped[str] = mapped_column(String(200), nullable=False)          # 首条文本截断或"图片分析"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("session_id", name="uq_chat_session_session_id"),
        Index("idx_chat_session_username", "username"),
        Index("idx_chat_session_updated", text("updated_at DESC")),
    )

    def __repr__(self) -> str:
        return f"<ChatSession {self.session_id} owner={self.username} title={self.title!r}>"


class ChatMessage(Base):
    """消息表 t_chat_message。

    message_id 唯一；session_id 关联 t_chat_session.session_id（不是主键 id），
    级联删除：删会话连带删消息。payload 存结构化展示块（JSONB），不含思维链。
    """

    __tablename__ = "t_chat_message"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    message_id: Mapped[str] = mapped_column(String(64), nullable=False)      # 业务编号 cm_xxx
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("t_chat_session.session_id", ondelete="CASCADE", name="fk_chat_message_session"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)            # user / assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)               # 文本内容（图片消息可为代表文本）
    content_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'text'")
    )  # text|image|detection|event_list|stats|dispatch_suggest|error
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite")
    )  # 结构化展示块（检测框/卡片），不含思维链
    run_id: Mapped[str | None] = mapped_column(String(64))                   # 关联派单 run（确认派单后回填）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("message_id", name="uq_chat_message_message_id"),
        CheckConstraint(
            "role IN (" + ", ".join(f"'{v}'" for v in ChatRole.ALL) + ")",
            name="chk_chat_message_role",
        ),
        Index("idx_chat_message_session", "session_id", "id"),
    )

    def __repr__(self) -> str:
        return f"<ChatMessage {self.message_id} session={self.session_id} [{self.role}]>"
