import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.models import Meeting, MeetingAccess, MeetingDOMSegment, MeetingRole, LiveSession, User
from app.repositories import (
    LiveSessionRepository,
    MeetingAccessRepository,
    MeetingDOMSegmentRepository,
    MeetingRepository,
)


def make_test_user(db: Session, suffix: str = "1") -> User:
    user = User(
        google_user_id=f"google-user-m1-{suffix}",
        email=f"user-m1-{suffix}@example.com",
        display_name=f"User M1 {suffix}",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_m1_tables_exist_in_database(db_session: Session) -> None:
    """Verify meeting_access and meeting_dom_segments tables exist with expected columns."""
    inspector = inspect(db_session.bind)
    tables = inspector.get_table_names()

    assert "meeting_access" in tables
    assert "meeting_dom_segments" in tables

    # Check meetings columns
    meeting_cols = {c["name"] for c in inspector.get_columns("meetings")}
    assert "conference_identity" in meeting_cols
    assert "grace_period_expires_at" in meeting_cols
    assert "last_heartbeat_at" in meeting_cols

    # Check live_sessions columns
    live_sess_cols = {c["name"] for c in inspector.get_columns("live_sessions")}
    assert "last_heartbeat_at" in live_sess_cols
    assert "client_server_offset_ms" in live_sess_cols


def test_m1_meeting_access_crud_and_rbac(db_session: Session) -> None:
    """Verify MeetingAccess model and repository operations."""
    user = make_test_user(db_session, suffix="access")
    meeting_repo = MeetingRepository(db_session)
    access_repo = MeetingAccessRepository(db_session)

    # Create meeting
    meeting = meeting_repo.create({
        "user_id": user.id,
        "title": "M1 Test Meeting",
        "conference_identity": "test-room-m1",
        "status": "in_progress",
    })

    # Add owner access
    access = access_repo.add_or_update_access(
        meeting_id=meeting.id,
        user_id=user.id,
        role=MeetingRole.OWNER.value,
    )
    assert access.role == MeetingRole.OWNER.value
    assert access.meeting_id == meeting.id
    assert access.user_id == user.id

    # Verify query
    role = access_repo.get_user_role(meeting.id, user.id)
    assert role == MeetingRole.OWNER.value

    # Verify access-aware meeting list
    items, total = meeting_repo.list_by_user(user_id=user.id)
    assert total >= 1
    assert any(m.id == meeting.id for m in items)


def test_m1_meeting_dom_segment_bulk_insert_and_idempotency(db_session: Session) -> None:
    """Verify MeetingDOMSegment bulk insert and idempotency (Never Delete Raw)."""
    user = make_test_user(db_session, suffix="dom")
    meeting_repo = MeetingRepository(db_session)
    session_repo = LiveSessionRepository(db_session)
    dom_repo = MeetingDOMSegmentRepository(db_session)

    meeting = meeting_repo.create({
        "user_id": user.id,
        "title": "DOM Test Meeting",
        "status": "in_progress",
    })
    session, _ = session_repo.create_or_get_or_supersede_session(
        meeting_id=meeting.id,
        user_id=user.id,
        tab_session_uuid=uuid.uuid4(),
    )

    segments_data = [
        {
            "sequence": 1,
            "speaker_name": "Alice",
            "text": "Xin chào mọi người",
            "observed_start_epoch_ms": 1700000000000,
            "observed_end_epoch_ms": 1700000002000,
            "is_final": True,
        },
        {
            "sequence": 2,
            "speaker_name": "Bob",
            "text": "Chào Alice",
            "observed_start_epoch_ms": 1700000002500,
            "observed_end_epoch_ms": 1700000004000,
            "is_final": True,
        },
    ]

    # First insert
    inserted = dom_repo.bulk_insert_segments(
        meeting_id=meeting.id,
        session_id=session.session_id,
        segments=segments_data,
    )
    assert len(inserted) == 2
    assert dom_repo.get_segment_count_by_session(session.session_id) == 2
    assert dom_repo.get_segment_count_by_meeting(meeting.id) == 2

    # Repeat insert (idempotency check)
    dom_repo.bulk_insert_segments(
        meeting_id=meeting.id,
        session_id=session.session_id,
        segments=segments_data,
    )
    # Count must remain 2 (no duplicate rows)
    assert dom_repo.get_segment_count_by_session(session.session_id) == 2


def test_m1_heartbeat_and_active_session_query(db_session: Session) -> None:
    """Verify heartbeat update and active session count."""
    user = make_test_user(db_session, suffix="hb")
    meeting_repo = MeetingRepository(db_session)
    session_repo = LiveSessionRepository(db_session)

    meeting = meeting_repo.create({
        "user_id": user.id,
        "title": "Heartbeat Test Meeting",
        "status": "in_progress",
    })
    session, _ = session_repo.create_or_get_or_supersede_session(
        meeting_id=meeting.id,
        user_id=user.id,
        tab_session_uuid=uuid.uuid4(),
    )

    client_ms = 1700000000000
    server_ms = 1700000000050  # 50ms drift

    updated_session, offset = session_repo.update_heartbeat(
        session_id=session.session_id,
        client_timestamp_ms=client_ms,
        server_timestamp_ms=server_ms,
    )
    assert updated_session is not None
    assert offset == 50
    assert updated_session.client_server_offset_ms == 50
    assert updated_session.last_heartbeat_at is not None

    active_count = session_repo.count_active_sessions_for_meeting(meeting.id, ttl_seconds=90)
    assert active_count == 1
