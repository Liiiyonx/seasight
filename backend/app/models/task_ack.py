"""ACK 审计账本 ORM 模型 —— t_task_ack（WP-14D）。

表结构冻结自《Harness 分工执行手册》WP-14D「冻结表契约」：

    t_task_ack (
        id, command_id, task_id, device_id, seq, outcome, accepted,
        reason, mode, received_at, received_wall_at, duplicate_count,
        last_duplicate_at, raw_payload, last_payload, created_at, updated_at)

设计要点：
- **一 command_id 一条规范回执**：``command_id`` 唯一（``uq_task_ack_command_id``），
  首次到达创建规范行（``outcome`` 取 WP-14C AckTracker 判定结果），后续重复
  到达只累加 ``duplicate_count`` 并更新 ``last_duplicate_at / last_payload``，
  绝不覆盖首次规范回执（``raw_payload`` 只写一次）。
- **审计留档**：``raw_payload`` 保存首次规范回执原文，供事后解释
  「平台是否收到过回执、何时收到、设备是否接受、为什么拒绝」；
  查询接口不回传该隐私字段（见 app.schemas.TaskAckOut）。
- ``received_at`` 为设备回执时间（冻结信封字段），``received_wall_at`` 为
  平台首次落库时间（默认 now()）—— 两者都可能与平台当前时钟不同，
  分开保存便于审计时间线。
- 外键关联 ``t_task.task_id``（``ON DELETE CASCADE``）：任务删除时其 ACK
  审计行一并清理，不留孤儿。

★ 双轨制（与 t_agent_run_state 一致）：
   · 新环境由 backend/db/init/01_schema.sql（总控集成）幂等建表 +
     `alembic stamp head`；
   · 存量环境由增量迁移 20260919_1700_task_ack 建表。
   字段/约束/索引命名与迁移及 01_schema.sql 完全一致，避免
   Alembic autogenerate 漂移。

★ SQLite 兼容（测试）：``BigInteger`` 主键用 ``with_variant(Integer, "sqlite")``
  使内存测试可建表；JSONB 用 ``with_variant(JSON, "sqlite")`` —— PostgreSQL
  上仍是 JSONB，SQLite 上降级为 JSON（TEXT），保证「无 PostgreSQL 也能跑」
  的持久化测试成立（模式与 test_agent_persistent_repository 一致）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TaskAck(Base):
    """ACK 审计账本：一命令一条规范回执 + 重复统计。"""

    __tablename__ = "t_task_ack"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    command_id: Mapped[str] = mapped_column(String(96), nullable=False)  # 一命令一条规范回执
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("t_task.task_id", ondelete="CASCADE", name="fk_task_ack_task"),
        nullable=False,
    )
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)   # new/duplicate/late/out_of_order
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(128))
    mode: Mapped[str | None] = mapped_column(String(32))
    received_at: Mapped[datetime] = mapped_column(  # 设备回执时间（冻结信封字段）
        DateTime(timezone=True), nullable=False
    )
    received_wall_at: Mapped[datetime] = mapped_column(  # 平台首次落库时间
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    duplicate_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_duplicate_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict] = mapped_column(   # 首次规范回执原文（审计留档）
        JSONB().with_variant(JSON, "sqlite"), nullable=False
    )
    last_payload: Mapped[dict | None] = mapped_column(  # 最近一次重复回执原文
        JSONB().with_variant(JSON, "sqlite")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("command_id", name="uq_task_ack_command_id"),
        # 冻结索引：task 维度按平台落库时间倒序；设备水位按 seq 倒序；
        # outcome 维度按落库时间倒序。
        Index("idx_task_ack_task", "task_id", desc("received_wall_at")),
        Index("idx_task_ack_device_seq", "device_id", desc("seq")),
        Index("idx_task_ack_outcome", "outcome", desc("received_wall_at")),
    )

    def __repr__(self) -> str:
        return (
            f"<TaskAck {self.command_id} outcome={self.outcome} "
            f"accepted={self.accepted} dup={self.duplicate_count}>"
        )
