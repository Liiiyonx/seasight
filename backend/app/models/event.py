"""识别事件 ORM 模型 —— 平台核心表。"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SAEnum,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class WasteClass:
    """垃圾类别常量（4 类，贴合连江养殖区实际）。"""

    FOAM = "foam"                  # EPS 泡沫浮球及碎片 —— 政策重点，治理优先级最高
    PLASTIC = "plastic"            # 塑胶浮球、塑料瓶、塑料袋
    FISHING_GEAR = "fishing_gear"  # 废旧渔网、绳索、饵料包装袋
    OTHER = "other"                # 木板、生活杂物、藻类聚集

    ALL = (FOAM, PLASTIC, FISHING_GEAR, OTHER)
    # 高优先级类别（触发自动派单）
    HIGH_PRIORITY = (FOAM, FISHING_GEAR)
    # 中文显示名
    LABELS = {
        FOAM: "泡沫类",
        PLASTIC: "塑胶类",
        FISHING_GEAR: "渔具类",
        OTHER: "其他",
    }


class EventStatus:
    NEW = "new"                  # 待处理（触发派单）
    DISPATCHED = "dispatched"    # 已派单
    RESOLVED = "resolved"        # 已清理
    IGNORED = "ignored"          # 人工忽略（误报）

    ALL = (NEW, DISPATCHED, RESOLVED, IGNORED)


class Event(Base):
    """识别事件表。

    设计要点：
    - (device_id, seq) 唯一约束：杜绝 MQTT 重传导致的重复派单
    - event_time + location 建索引：热力图查询直接受益
    """

    __tablename__ = "t_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    location: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), nullable=False
    )
    main_class: Mapped[str] = mapped_column(
        SAEnum(*WasteClass.ALL, name="waste_class_enum", create_type=False),
        nullable=False,
        index=True,
    )
    det_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    max_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        SAEnum(*EventStatus.ALL, name="event_status_enum", create_type=False),
        nullable=False,
        default=EventStatus.NEW,
        index=True,
    )
    # 乡镇归属：入库时由 location 最近邻计算（app.core.geo.nearest_township）。
    # 存字段而非每次查询现算，是为了让 operator 的「本辖区」过滤能走索引。
    township: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("device_id", "seq", name="uq_event_device_seq"),
        Index("idx_event_location", "location", postgresql_using="gist"),
        Index("idx_event_time_class", "event_time", "main_class"),
    )

    def __repr__(self) -> str:
        return f"<Event {self.event_id} {self.main_class} from {self.device_id}>"
