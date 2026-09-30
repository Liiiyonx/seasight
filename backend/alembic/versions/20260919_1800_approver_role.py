"""add approver role for agent approval

Revision ID: 20260919_1800_approver_role
Revises: 20260919_1700_task_ack
Create Date: 2026-09-19 18:00:00

The Agent approval API already separates approval authority from task write
authority: admin and approver may decide approvals, while only admin and
operator may create or update tasks. The database and token role whitelist
must carry the same approver role or valid approval decisions are rejected as
anonymous requests.

This migration appends ``approver`` before ``viewer`` so the enum order stays
aligned with the fresh-install schema. PostgreSQL cannot remove a single enum
value safely in place, so downgrade is intentionally a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260919_1800_approver_role"
down_revision: str | None = "20260919_1700_task_ack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow the dedicated approver role for Agent approval decisions."""
    op.execute(
        "ALTER TYPE user_role_enum "
        "ADD VALUE IF NOT EXISTS 'approver' BEFORE 'viewer'"
    )


def downgrade() -> None:
    """Keep the enum value; PostgreSQL cannot safely remove one in place."""
    pass
