"""clear unattributed coverage values after introducing availability metadata

Revision ID: 20260919_1300_clear_coverage
Revises: 20260919_1200_coverage_nullable
Create Date: 2026-09-19 13:00:00

The nullable migration preserved legacy numeric values and marked every
existing row as ``not_available`` because their provenance cannot be proven.
Keeping those numbers visible still presents unverified areas as if they were
measured. This data migration clears only rows explicitly marked
``not_available``; rows marked ``available`` or ``partial`` are untouched.

The correction is intentionally not reversible: once an unattributed value is
cleared, its original bytes cannot be reconstructed honestly. Downgrading this
revision is therefore a no-op; the preceding schema migration still supports
structural downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260919_1300_clear_coverage"
down_revision: str | None = "20260919_1200_coverage_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Clear numeric coverage only when provenance is explicitly unavailable."""
    op.execute(
        """
        UPDATE t_report_daily
        SET coverage_area = NULL
        WHERE coverage_availability = 'not_available'
          AND coverage_area IS NOT NULL
        """
    )


def downgrade() -> None:
    """Keep the correction; cleared unattributed values cannot be restored honestly."""
    pass
