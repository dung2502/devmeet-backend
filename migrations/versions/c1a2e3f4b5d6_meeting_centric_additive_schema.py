"""meeting centric additive schema

Revision ID: c1a2e3f4b5d6
Revises: b3e4f7a18c90
Create Date: 2026-09-15 17:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c1a2e3f4b5d6'
down_revision: str | None = 'b3e4f7a18c90'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create meeting_access table
    op.create_table(
        'meeting_access',
        sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('meeting_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('role', sa.String(length=50), nullable=False, server_default='PARTICIPANT'),
        sa.Column('first_joined_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('meeting_id', 'user_id', name='uq_meeting_access_meeting_user'),
    )
    op.create_index('idx_meeting_access_meeting_id', 'meeting_access', ['meeting_id'], unique=False)
    op.create_index('idx_meeting_access_user_meeting', 'meeting_access', ['user_id', 'meeting_id'], unique=False)

    # 2. Create meeting_dom_segments table
    op.create_table(
        'meeting_dom_segments',
        sa.Column('segment_id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('meeting_id', sa.UUID(), nullable=False),
        sa.Column('session_id', sa.UUID(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('speaker_name', sa.String(length=255), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('start_time_offset_ms', sa.BigInteger(), nullable=True),
        sa.Column('end_time_offset_ms', sa.BigInteger(), nullable=True),
        sa.Column('observed_start_epoch_ms', sa.BigInteger(), nullable=False),
        sa.Column('observed_end_epoch_ms', sa.BigInteger(), nullable=False),
        sa.Column('is_final', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('confidence_score', sa.Float(), nullable=False, server_default=sa.text('1.0')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_id'], ['live_sessions.session_id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('segment_id'),
        sa.UniqueConstraint('session_id', 'sequence', name='uq_meeting_dom_segments_session_sequence'),
    )
    op.create_index('idx_meeting_dom_segments_meeting_epoch', 'meeting_dom_segments', ['meeting_id', 'observed_start_epoch_ms'], unique=False)
    op.create_index('idx_meeting_dom_segments_session_id', 'meeting_dom_segments', ['session_id'], unique=False)

    # 3. Add additive columns to meetings
    op.add_column('meetings', sa.Column('conference_identity', sa.String(length=255), nullable=True))
    op.create_index('idx_meetings_conference_identity', 'meetings', ['conference_identity'], unique=False)
    op.add_column('meetings', sa.Column('grace_period_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('meetings', sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True))

    # 4. Add additive columns to live_sessions
    op.add_column('live_sessions', sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('live_sessions', sa.Column('client_server_offset_ms', sa.BigInteger(), nullable=False, server_default=sa.text('0')))


def downgrade() -> None:
    # 1. Drop additive columns from live_sessions
    op.drop_column('live_sessions', 'client_server_offset_ms')
    op.drop_column('live_sessions', 'last_heartbeat_at')

    # 2. Drop additive columns from meetings
    op.drop_column('meetings', 'last_heartbeat_at')
    op.drop_column('meetings', 'grace_period_expires_at')
    op.drop_index('idx_meetings_conference_identity', table_name='meetings')
    op.drop_column('meetings', 'conference_identity')

    # 3. Drop meeting_dom_segments
    op.drop_index('idx_meeting_dom_segments_session_id', table_name='meeting_dom_segments')
    op.drop_index('idx_meeting_dom_segments_meeting_epoch', table_name='meeting_dom_segments')
    op.drop_table('meeting_dom_segments')

    # 4. Drop meeting_access
    op.drop_index('idx_meeting_access_user_meeting', table_name='meeting_access')
    op.drop_index('idx_meeting_access_meeting_id', table_name='meeting_access')
    op.drop_table('meeting_access')
