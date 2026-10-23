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

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260905_0100_servo_telemetry"
down_revision: str | None = "20260922_1000_chat_session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ★ 幂等写法（双轨制的硬要求，与 20260919_1200_coverage_nullable 同口径）：
    #   轨道 A 的 01_schema.sql 已经带 servo_telemetry 列，而本迁移会在
    #   「01_schema.sql 建库 → upgrade head」的流程里被执行（生产
    #   `make prod-migrate-stamp/prod-migrate`、以及迁移测试的 scratch 库
    #   都走这条路）。普通 add_column 会直接 DuplicateColumn 报错，
    #   把整条迁移链卡死在最后一步。
    op.execute("ALTER TABLE t_track ADD COLUMN IF NOT EXISTS servo_telemetry JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE t_track DROP COLUMN IF EXISTS servo_telemetry")
