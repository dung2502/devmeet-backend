"""add platform to meetings

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d8e9
Create Date: 2026-09-24 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = 'f4a5b6c7d8e9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add platform column with default 'GOOGLE_MEET'
    op.add_column(
        'meetings',
        sa.Column('platform', sa.String(length=20), nullable=False, server_default='GOOGLE_MEET'),
    )

    # 2. Index on platform for fast filtering and partition queries
    op.create_index(
        'idx_meetings_platform',
        'meetings',
        ['platform'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('idx_meetings_platform', table_name='meetings')
    op.drop_column('meetings', 'platform')
