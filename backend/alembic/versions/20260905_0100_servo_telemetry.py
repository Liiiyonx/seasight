"""为 t_track 增加逐舵机遥测列（servo_telemetry JSONB）

背景（2026-10-05 精读厂商资料后）：机械臂驱动原先只上报 status/battery，
但舵机本身能回读电压/温度/位置 —— 那是「机械臂真的动了」的硬证据，
比平台自报的 status 更有说服力。字段落在 t_track（轨迹/遥测表），
与 battery/speed 同级。

命名与 app/models/misc.py 及 db/init/01_schema.sql 保持一致，
避免 Alembic autogenerate 漂移。JSONB 而非 JSON：支持 ->> 索引，
且与 chat_session.payload 等既有列类型一致。

revision id 取 26 字符（20260905_0100_servo_telemetry），不超过
alembic_version.version_num 的 VARCHAR(32)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260905_0100_servo_telemetry"
down_revision: str | None = "20260922_1000_chat_session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_track",
        sa.Column("servo_telemetry", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("t_track", "servo_telemetry")
