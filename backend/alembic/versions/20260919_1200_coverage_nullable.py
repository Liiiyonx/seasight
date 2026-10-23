"""coverage_area 可空化 + coverage_availability 可用性列（WP-07 数据完整性）

Revision ID: 20260919_1200_coverage_nullable
Revises: 20260918_1100_agent_runtime
Create Date: 2026-09-19 12:00:00

★ 语义：coverage_area 此前 NOT NULL DEFAULT 0，聚合层把它当作「假实测」写 0。
  WP-07 改为可空：NULL = 未统计（not_available），与「真实 0」结构区分；
  未统计禁止以 0 冒充实测。

★ upgrade：
  · t_report_daily.coverage_area 去掉 NOT NULL 与 DEFAULT 0（可空）；
  · 新增 coverage_availability VARCHAR(16) NOT NULL DEFAULT 'not_available'
    （三态 available / partial / not_available，判定见
    app.services.report.coverage_availability / coverage_summary_availability）。
  存量行的 coverage_area 保持原值不破坏；可用性统一标记 not_available。
  后续 20260919_1300_clear_coverage 会把所有 not_available 数值清为 NULL，
  避免无来源面积继续以实测值展示。

★ downgrade（恢复原状但不破坏数据）：
  · 先把 NULL 回填为 0（否则 NOT NULL 约束无法恢复；回退后该 0 是占位语义，
    不再是「可空未统计」结构）；
  · 删除 coverage_availability 列；
  · 恢复 coverage_area NOT NULL DEFAULT 0。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# ★ 注意：revision id 不得超过 32 字符（alembic_version.version_num VARCHAR(32)）。
revision: str = "20260919_1200_coverage_nullable"
down_revision: str | None = "20260918_1100_agent_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """coverage_area 可空 + 新增 coverage_availability 列（幂等，双轨安全）。"""
    # 1) coverage_area 可空，去掉 DEFAULT 0 —— NULL=未统计，与「真实 0」区分
    #    显式传 existing_*：不做 DB 对比，直接 DROP NOT NULL + DROP DEFAULT，
    #    在「旧库 NOT NULL DEFAULT 0」与「新库已可空」两种状态都安全。
    op.alter_column(
        "t_report_daily",
        "coverage_area",
        existing_type=sa.Numeric(12, 2),
        nullable=True,
        existing_server_default=sa.text("0"),
        server_default=None,
    )
    # 2) 新增可用性三态列（存量行统一 not_available，不回溯推断来源）。
    #    ADD COLUMN IF NOT EXISTS：存量库（迁移执行）与全新库
    #    （01_schema.sql 已建列、仅 alembic stamp head）双轨都安全。
    op.execute(
        "ALTER TABLE t_report_daily "
        "ADD COLUMN IF NOT EXISTS coverage_availability "
        "VARCHAR(16) NOT NULL DEFAULT 'not_available'"
    )


def downgrade() -> None:
    """恢复原状：NULL 回填 0 → 删除 availability 列 → 恢复 NOT NULL DEFAULT 0。"""
    # 1) NULL 回填为 0（占位语义；回退后不再保留「可空未统计」结构）
    op.execute("UPDATE t_report_daily SET coverage_area = 0 WHERE coverage_area IS NULL")
    # 2) 删除可用性列
    op.drop_column("t_report_daily", "coverage_availability")
    # 3) 恢复 NOT NULL DEFAULT 0
    op.alter_column(
        "t_report_daily",
        "coverage_area",
        existing_type=sa.Numeric(12, 2),
        nullable=False,
        server_default=sa.text("0"),
    )
