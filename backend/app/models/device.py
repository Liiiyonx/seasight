"""设备 ORM 模型。"""

from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SAEnum,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class DeviceType(str):
    """设备类型常量。"""

    SHORE_CAMERA = "shore_camera"
    DRONE = "drone"
    ROBOT = "robot"


class DeviceStatus(str):
    ONLINE = "online"
    OFFLINE = "offline"
    FAULT = "fault"


class Device(Base):
    """设备表：岸基摄像头 / 无人机 / 水面机器人。"""

    __tablename__ = "t_device"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    device_type: Mapped[str] = mapped_column(
        SAEnum(
            "shore_camera", "drone", "robot",
            name="device_type_enum", create_type=False,
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # PostGIS 空间字段：POINT(lng lat), SRID 4326 (WGS84)
    location: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        SAEnum("online", "offline", "fault", name="device_status_enum", create_type=False),
        nullable=False,
        default=DeviceStatus.OFFLINE,
    )
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stream_url: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_device_location", "location", postgresql_using="gist"),
    )

    def __repr__(self) -> str:
        return f"<Device {self.device_id} ({self.device_type})>"
