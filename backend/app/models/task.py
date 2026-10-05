"""清理任务（工单）ORM 模型 —— 含状态机定义。"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TaskStatus:
    """任务状态机。

    合法流转：
        pending → assigned → navigating → collecting → done
        任意态 → cancelled

    ★ 所有状态变更必须走 services.dispatch 层，禁止直接 UPDATE。
    """

    PENDING = "pending"          # 待派单
    ASSIGNED = "assigned"        # 已派单（等机器人 ACK）
    NAVIGATING = "navigating"    # 前往目标点
    COLLECTING = "collecting"    # 作业中
    DONE = "done"                # 已完成
    CANCELLED = "cancelled"      # 已取消

    ALL = (PENDING, ASSIGNED, NAVIGATING, COLLECTING, DONE, CANCELLED)

    # 合法状态迁移表：{当前状态: {允许的目标状态}}
    TRANSITIONS: dict[str, set[str]] = {
        PENDING: {ASSIGNED, CANCELLED},
        ASSIGNED: {NAVIGATING, PENDING, CANCELLED},   # 回退到 PENDING：ACK 超时重新派单
        NAVIGATING: {COLLECTING, CANCELLED},
        COLLECTING: {DONE, CANCELLED},
        DONE: set(),                                   # 终态
        CANCELLED: set(),                              # 终态
    }

    LABELS = {
        PENDING: "待派单",
        ASSIGNED: "已派单",
        NAVIGATING: "前往中",
        COLLECTING: "作业中",
        DONE: "已完成",
        CANCELLED: "已取消",
    }

    # 活跃状态（未终结）——用于防重复派单判断
    ACTIVE = (PENDING, ASSIGNED, NAVIGATING, COLLECTING)

    @classmethod
    def can_transition(cls, current: str, target: str) -> bool:
        """判断状态迁移是否合法。"""
        return target in cls.TRANSITIONS.get(current, set())


class TaskPriority:
    """任务优先级 —— 由事件的垃圾类别决定。

    ★ 这是优先级的**唯一真源**。派单引擎建任务、类别加权判断、
    前端筛选都引用这里，不允许任何地方再写 `1 if ... else 3` 的字面量
    —— 那样改了策略只改一处，三处就会静默不一致。

    分级依据：泡沫类（`foam`）体积大、易扩散、清理时效性强，
    定级 1（紧急）；其余类别定级 3（普通）。
    """

    URGENT = 1      # 高优先级类别（如泡沫）
    NORMAL = 3      # 其余类别

    # 数值越小越紧急；校验范围与 DB CheckConstraint 一致
    MIN = 1
    MAX = 9

    @classmethod
    def for_waste_class(cls, main_class: str) -> int:
        """按垃圾类别给出优先级。

        引用 models.event.WasteClass.HIGH_PRIORITY 而不是硬编码类别名，
        保证「策略调整」只需改 HIGH_PRIORITY 一处。
        """
        from app.models.event import WasteClass

        return cls.URGENT if main_class in WasteClass.HIGH_PRIORITY else cls.NORMAL


class ReviewResult:
    PENDING = "pending"
    CONFIRMED = "confirmed"      # 复核确认存在垃圾
    NOT_FOUND = "not_found"      # 到场未发现（疑似误报）
    RECHECK = "recheck"          # 需人工复查

    ALL = (PENDING, CONFIRMED, NOT_FOUND, RECHECK)


class Task(Base):
    """清理任务表（工单表）。

    设计要点：
    - 与事件表分离：一次事件可能派生多个任务（聚集区太大需多趟）
    - status 为状态机字段，变更走服务层校验
    - 部分唯一索引防止同一事件重复派单（见 01_schema.sql）
    """

    __tablename__ = "t_task"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    event_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("t_event.event_id", ondelete="SET NULL"), index=True
    )
    robot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    target_location: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), nullable=False
    )
    status: Mapped[str] = mapped_column(
        SAEnum(*TaskStatus.ALL, name="task_status_enum", create_type=False),
        nullable=False,
        default=TaskStatus.PENDING,
        index=True,
    )
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)

    # 乡镇归属：由 target_location 最近邻计算，供 operator 辖区过滤
    township: Mapped[str | None] = mapped_column(String(64), index=True)

    # 时间戳链：完整记录任务生命周期
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    collected_weight: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    review_result: Mapped[str] = mapped_column(
        SAEnum(*ReviewResult.ALL, name="review_result_enum", create_type=False),
        default=ReviewResult.PENDING,
    )
    remark: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("priority BETWEEN 1 AND 9", name="chk_task_priority"),
        Index("idx_task_target", "target_location", postgresql_using="gist"),
    )

    def __repr__(self) -> str:
        return f"<Task {self.task_id} [{self.status}] robot={self.robot_id}>"
