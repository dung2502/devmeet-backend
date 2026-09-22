"""add previous_refresh_token_hash to auth_sessions

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-16 14:30:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e3f4a5b6c7d8'
down_revision: str | None = 'd2e3f4a5b6c7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'auth_sessions',
        sa.Column('previous_refresh_token_hash', sa.String(length=255), nullable=True),
    )
    op.create_index(
        'idx_auth_sessions_prev_refresh_hash',
        'auth_sessions',
        ['previous_refresh_token_hash'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('idx_auth_sessions_prev_refresh_hash', table_name='auth_sessions')
    op.drop_column('auth_sessions', 'previous_refresh_token_hash')
