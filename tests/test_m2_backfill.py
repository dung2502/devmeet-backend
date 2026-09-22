import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Meeting, MeetingAccess, MeetingDOMSegment, MeetingRole, User
from scripts.backfill_meeting_centric import run_backfill


def make_test_user(db: Session, suffix: str = "m2") -> User:
    user = User(
        google_user_id=f"google-user-{suffix}",
        email=f"user-{suffix}@example.com",
        display_name=f"User {suffix}",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_m2_backfill_end_to_end_and_idempotency(db_session: Session) -> None:
    """Verify that backfill migrates meeting_access, conference_identity, and DOM segments, and is idempotent."""
    user = make_test_user(db_session, suffix="backfill_test")

    # 1. Create a meeting simulating legacy single-user meeting with dom_transcript_data
    meeting_legacy = Meeting(
        user_id=user.id,
        conference_record_name="conferenceRecords/test-m2-rec-1",
        meeting_space_name="spaces/test-m2-space-1",
        meeting_url="https://meet.google.com/abc-defg-hij",
        title="Legacy Meeting With DOM",
        start_time=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        status="completed",
        dom_capture_status="completed",
        dom_transcript_data={
            "session_id": str(uuid.uuid4()),
            "captured_at": "2026-09-15T10:30:00Z",
            "segments": [
                {
                    "segment_id": str(uuid.uuid4()),
                    "sequence": 1,
                    "speaker_name": "Speaker 1",
                    "text": "Bắt đầu cuộc họp.",
                    "start_time_offset_ms": 0,
                    "end_time_offset_ms": 2000,
                },
                {
                    "segment_id": str(uuid.uuid4()),
                    "sequence": 2,
                    "speaker_name": "Speaker 2",
                    "text": "Đồng ý, tôi sẽ trình bày báo cáo.",
                    "start_time_offset_ms": 2500,
                    "end_time_offset_ms": 5000,
                },
            ],
        },
    )
    db_session.add(meeting_legacy)
    db_session.commit()
    db_session.refresh(meeting_legacy)

    # 2. Run initial backfill
    res1 = run_backfill(db_session)
    assert res1["access_records_created"] >= 1
    assert res1["conference_identities_updated"] >= 1
    assert res1["dom_segments_ingested"] >= 2

    # Verify meeting_access was created with OWNER role
    access = db_session.scalar(
        select(MeetingAccess).where(
            MeetingAccess.meeting_id == meeting_legacy.id,
            MeetingAccess.user_id == user.id,
        )
    )
    assert access is not None
    assert access.role == MeetingRole.OWNER.value

    # Verify conference_identity was populated
    db_session.refresh(meeting_legacy)
    assert meeting_legacy.conference_identity == "conferenceRecords/test-m2-rec-1"

    # Verify meeting_dom_segments were created
    segments = list(
        db_session.scalars(
            select(MeetingDOMSegment).where(MeetingDOMSegment.meeting_id == meeting_legacy.id)
        )
    )
    assert len(segments) == 2
    assert segments[0].speaker_name == "Speaker 1"
    assert segments[0].text == "Bắt đầu cuộc họp."
    assert segments[1].speaker_name == "Speaker 2"

    # 3. Run backfill a SECOND time to verify 100% IDEMPOTENCY
    res2 = run_backfill(db_session)
    assert res2["access_records_created"] == 0
    assert res2["conference_identities_updated"] == 0

    # Total segments count should still be exactly 2 (no duplicate rows created)
    total_segments = list(
        db_session.scalars(
            select(MeetingDOMSegment).where(MeetingDOMSegment.meeting_id == meeting_legacy.id)
        )
    )
    assert len(total_segments) == 2
