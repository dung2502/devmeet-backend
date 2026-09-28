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
    bind = op.get_bind()
    insp = sa.inspect(bind)
    columns = [c['name'] for c in insp.get_columns('meetings')]
    if 'platform' not in columns:
        op.add_column(
            'meetings',
            sa.Column('platform', sa.String(length=20), nullable=False, server_default='GOOGLE_MEET'),
        )

    indexes = [idx['name'] for idx in insp.get_indexes('meetings')]
    if 'idx_meetings_platform' not in indexes:
        op.create_index(
            'idx_meetings_platform',
            'meetings',
            ['platform'],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    indexes = [idx['name'] for idx in insp.get_indexes('meetings')]
    if 'idx_meetings_platform' in indexes:
        op.drop_index('idx_meetings_platform', table_name='meetings')
    columns = [c['name'] for c in insp.get_columns('meetings')]
    if 'platform' in columns:
        op.drop_column('meetings', 'platform')
