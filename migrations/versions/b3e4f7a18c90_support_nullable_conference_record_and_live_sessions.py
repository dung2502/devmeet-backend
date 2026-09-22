"""support nullable conference_record and live_sessions

Revision ID: b3e4f7a18c90
Revises: ce42dbb568fe
Create Date: 2026-09-08 14:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b3e4f7a18c90'
down_revision: str | None = 'ce42dbb568fe'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Alter meetings.conference_record_name to nullable
    op.alter_column('meetings', 'conference_record_name',
               existing_type=sa.String(length=255),
               nullable=True)

    # 2. Create live_sessions table
    op.create_table('live_sessions',
        sa.Column('session_id', sa.UUID(), nullable=False),
        sa.Column('meeting_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('tab_session_uuid', sa.UUID(), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='ACTIVE', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('session_id'),
        sa.UniqueConstraint('user_id', 'meeting_id', 'tab_session_uuid', name='uq_live_sessions_user_meeting_tab')
    )
    op.create_index('idx_live_sessions_meeting_id', 'live_sessions', ['meeting_id'], unique=False)
    op.create_index('idx_live_sessions_user_id', 'live_sessions', ['user_id'], unique=False)
    op.create_index(
        'uq_live_sessions_active_user_meeting',
        'live_sessions',
        ['user_id', 'meeting_id'],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'")
    )


def downgrade() -> None:
    bind = op.get_bind()
    null_count = bind.execute(sa.text("SELECT COUNT(*) FROM meetings WHERE conference_record_name IS NULL")).scalar()
    if null_count and null_count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration 'b3e4f7a18c90': 'meetings' table contains {null_count} row(s) with NULL "
            f"conference_record_name. Preserving data - downgrade aborted."
        )

    op.drop_index('uq_live_sessions_active_user_meeting', table_name='live_sessions')
    op.drop_index('idx_live_sessions_user_id', table_name='live_sessions')
    op.drop_index('idx_live_sessions_meeting_id', table_name='live_sessions')
    op.drop_table('live_sessions')
    op.alter_column('meetings', 'conference_record_name',
               existing_type=sa.String(length=255),
               nullable=False)
