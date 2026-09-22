"""add partial unique index and shared room indexes to meetings

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-09-17 09:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'f4a5b6c7d8e9'
down_revision: str | None = 'e3f4a5b6c7d8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Partial Unique Index: only 1 in_progress meeting per conference_identity at any given time
    op.create_index(
        'uq_active_conference_identity',
        'meetings',
        ['conference_identity'],
        unique=True,
        postgresql_where=sa.text("status = 'in_progress' AND conference_identity IS NOT NULL"),
    )

    # 2. Composite index for rapid lookup of active room status & continuity
    op.create_index(
        'idx_meetings_identity_status',
        'meetings',
        ['conference_identity', 'status'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('idx_meetings_identity_status', table_name='meetings')
    op.drop_index('uq_active_conference_identity', table_name='meetings')
