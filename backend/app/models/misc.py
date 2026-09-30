"""作业轨迹、统计宽表、用户 ORM 模型。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Track(Base):
    """作业轨迹表（按日分区）。

    记录机器人作业轨迹点，用于回放与里程统计。
    """

    __tablename__ = "t_track"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    robot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[Any] = mapped_column(Geometry(geometry_type="POINT", srid=4326), nullable=False)
    battery: Mapped[int | None] = mapped_column(SmallInteger)

    # 三仓占用率（0~1）——「打捞即粗分三仓」设计的软件侧落点
    bin_foam: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    bin_plastic: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    bin_mixed: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))

    speed: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("idx_track_location", "location", postgresql_using="gist"),
    )


class ReportDaily(Base):
    """统计宽表（预聚合）。

    由定时任务每日凌晨聚合生成，报表页只读这张表——
    用存储换查询速度，即使事件量到百万级报表仍毫秒响应。
    """

    __tablename__ = "t_report_daily"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    township: Mapped[str | None] = mapped_column(String(64), index=True)
    main_class: Mapped[str | None] = mapped_column(
        SAEnum(
            "foam", "plastic", "fishing_gear", "other",
            name="waste_class_enum", create_type=False,
        )
    )
    event_count: Mapped[int] = mapped_column(default=0)
    task_count: Mapped[int] = mapped_column(default=0)
    done_count: Mapped[int] = mapped_column(default=0)
    collected_kg: Mapped[Decimal] = mapped_column(Numeric(10, 3), default=0)
    # WP-07：coverage_area 可空 —— NULL=未统计（无可靠数据源），与「真实 0」区分；
    # 未统计禁止以 0 冒充实测。可用性三态见 app.services.report.coverage_availability。
    coverage_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    coverage_availability: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="not_available",
        server_default=text("'not_available'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("stat_date", "township", "main_class", name="uq_report_daily"),
    )


class UserRole:
    """用户角色：admin=全部 / operator=本辖区写 / approver=审批 / viewer=只读。"""

    ADMIN = "admin"
    OPERATOR = "operator"
    APPROVER = "approver"
    VIEWER = "viewer"

    ALL = (ADMIN, OPERATOR, APPROVER, VIEWER)


class User(Base):
    """用户表。政务项目对审批和写权限敏感，角色分层不能省。"""

    __tablename__ = "t_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(
        SAEnum(*UserRole.ALL, name="user_role_enum", create_type=False),
        nullable=False,
        default=UserRole.VIEWER,
        index=True,
    )
    township_scope: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.role})>"


class AuditLog(Base):
    """操作审计日志 —— 谁在何时对什么做了什么。

    政务交付的可追溯性刚需：写操作（登录、建单、改状态、忽略事件）
    都落一条审计记录，供管理员回溯「这个工单是谁改成 done 的」。
    """

    __tablename__ = "t_audit_log"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.username} {self.action} {self.target_id}>"
