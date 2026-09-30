"""Agent 持久化续跑状态 ORM 模型 —— t_agent_run_state（WP-10）。

表结构与《Harness 分工执行手册》3.7 + WP-10 冻结接口逐字段对齐：

    t_agent_run_state (id, run_id, request_json, runtime_state_json,
                       idempotency_key, state_version, created_at, updated_at)

约束：
    · run_id 唯一（uq_agent_run_state_run_id）—— 与 t_agent_run 一一对应
    · idempotency_key 唯一**部分索引**（NULL 不参与唯一）——
      相同幂等键并发创建最多产生一个 run
    · (run_id, state_version) 用于乐观并发检查：保存 run 时若库内版本与
      调用方观察到的版本不一致，抛 TaskConflictError，不得静默覆盖
    · run_id 外键关联 t_agent_run.run_id（与 t_agent_step / t_agent_approval
      同款 ondelete=CASCADE）

★ 设计纪律（解密预算，与 agent.py 一致）：
   request_json / runtime_state_json 只存续跑所需的业务摘要与工具绑定，
   由 SqlAlchemyRunRepository 在写入前统一脱敏；禁止保存模型思维链、
   密钥、原始 Prompt 或未经脱敏的敏感载荷。

★ 双轨制：新环境由 backend/db/init/01_schema.sql（总控集成）幂等建表 +
   `alembic stamp head`；存量环境由增量迁移
   20260919_1600_agent_persistent_state 建表。字段/约束/索引命名与
   迁移及 01_schema.sql 完全一致，避免 Alembic autogenerate 漂移。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AgentRunState(Base):
    """续跑状态表 t_agent_run_state。

    request_json / runtime_state_json 只承载「重新拉起 AgentRuntime.resume()」
    所需的最小续跑上下文（已脱敏），state_version 提供乐观并发版本号。
    """

    __tablename__ = "t_agent_run_state"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("t_agent_run.run_id", ondelete="CASCADE", name="fk_agent_run_state_run"),
        nullable=False,
    )
    request_json: Mapped[str] = mapped_column(Text, nullable=False)          # 已脱敏请求摘要
    runtime_state_json: Mapped[str] = mapped_column(Text, nullable=False)    # 已脱敏续跑状态
    idempotency_key: Mapped[str | None] = mapped_column(String(128))         # 幂等键（NULL 不参与唯一）
    state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_agent_run_state_run_id"),
        # 幂等键唯一部分索引：PostgreSQL 上为 WHERE idempotency_key IS NOT NULL；
        # SQLite（测试）渲染为普通 UNIQUE 索引，其 NULL 语义天然不参与唯一。
        Index(
            "uq_agent_run_state_idem_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # 乐观并发检查 (run_id, state_version) 的检索索引
        Index("idx_agent_run_state_version", "run_id", "state_version"),
    )

    def __repr__(self) -> str:
        return f"<AgentRunState {self.run_id} v{self.state_version}>"
