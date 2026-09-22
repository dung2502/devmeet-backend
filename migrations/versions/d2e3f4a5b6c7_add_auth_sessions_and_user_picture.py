"""add auth_sessions and user picture

Revision ID: d2e3f4a5b6c7
Revises: c1a2e3f4b5d6
Create Date: 2026-09-16 14:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd2e3f4a5b6c7'
down_revision: str | None = 'c1a2e3f4b5d6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add picture column to users table
    op.add_column('users', sa.Column('picture', sa.String(length=1024), nullable=True))

    # 2. Create auth_sessions table
    op.create_table(
        'auth_sessions',
        sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('client_type', sa.String(length=20), nullable=False, server_default='WEB'),
        sa.Column('refresh_token_hash', sa.String(length=255), nullable=False),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_auth_sessions_user_id', 'auth_sessions', ['user_id'], unique=False)
    op.create_index('idx_auth_sessions_refresh_hash', 'auth_sessions', ['refresh_token_hash'], unique=False)
    op.create_index('idx_auth_sessions_expires_revoked', 'auth_sessions', ['expires_at', 'revoked_at'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_auth_sessions_expires_revoked', table_name='auth_sessions')
    op.drop_index('idx_auth_sessions_refresh_hash', table_name='auth_sessions')
    op.drop_index('idx_auth_sessions_user_id', table_name='auth_sessions')
    op.drop_table('auth_sessions')
    op.drop_column('users', 'picture')
