import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.models import LiveSession, LiveSessionStatus, Meeting, MeetingDOMSegment, MeetingRole, User
from app.repositories import LiveSessionRepository, MeetingDOMSegmentRepository, MeetingRepository
from app.services.dom_aggregation_service import DOMAggregationService
from app.services.transcript_view_service import TranscriptViewService


def create_user(db: Session, email: str) -> User:
    user = User(
        google_user_id=f"gid-{email}",
        email=email,
        display_name=email.split("@")[0].capitalize(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_session(db: Session, meeting_id: uuid.UUID, user_id: uuid.UUID, offset_ms: int = 0) -> LiveSession:
    sess = LiveSession(
        session_id=uuid.uuid4(),
        meeting_id=meeting_id,
        user_id=user_id,
        tab_session_uuid=uuid.uuid4(),
        status=LiveSessionStatus.COMPLETED.value,
        client_server_offset_ms=offset_ms,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def test_m4_single_session_aggregation(db_session: Session) -> None:
    """Verify single-session DOM aggregation formats entries and calculates coverage."""
    user = create_user(db_session, "user1@example.com")
    meeting_repo = MeetingRepository(db_session)
    dom_repo = MeetingDOMSegmentRepository(db_session)
    agg_service = DOMAggregationService(db_session)

    meeting = meeting_repo.create({
        "user_id": user.id,
        "title": "M4 Single Session",
        "start_time": datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        "status": "completed",
    })

    sess = create_session(db_session, meeting.id, user.id)
    segments = [
        {
            "sequence": 1,
            "speaker_name": "Speaker A",
            "text": "Phần đầu tiên của buổi họp.",
            "observed_start_epoch_ms": 1700000000000,
            "observed_end_epoch_ms": 1700000002000,
        },
        {
            "sequence": 2,
            "speaker_name": "Speaker A",
            "text": "Chúng ta xem xét báo cáo quý 3.",
            "observed_start_epoch_ms": 1700000003000,
            "observed_end_epoch_ms": 1700000005000,
        },
        {
            "sequence": 3,
            "speaker_name": "Speaker B",
            "text": "Tôi đồng ý với các số liệu này.",
            "observed_start_epoch_ms": 1700000006000,
            "observed_end_epoch_ms": 1700000008000,
        },
    ]

    dom_repo.bulk_insert_segments(meeting.id, sess.session_id, segments)

    result = agg_service.compute_aggregated_view(meeting.id)
    assert result.total_raw_segments == 3
    # Speaker A consecutive utterances merged within 3s gap -> 2 utterances total
    assert result.total_aggregated_entries == 2
    assert result.entries[0].speaker == "Speaker A"
    assert "Phần đầu tiên của buổi họp." in result.entries[0].text
    assert "Chúng ta xem xét báo cáo quý 3." in result.entries[0].text
    assert result.entries[1].speaker == "Speaker B"
    assert result.cross_session_coverage > 0.0


def test_m4_multi_session_deduplication(db_session: Session) -> None:
    """Verify that multi-client overlapping speech is deduplicated without double phrases."""
    user_a = create_user(db_session, "client_a@example.com")
    user_b = create_user(db_session, "client_b@example.com")
    meeting_repo = MeetingRepository(db_session)
    dom_repo = MeetingDOMSegmentRepository(db_session)
    agg_service = DOMAggregationService(db_session)

    meeting = meeting_repo.create({
        "user_id": user_a.id,
        "title": "M4 Overlap Test",
        "status": "completed",
    })

    sess_a = create_session(db_session, meeting.id, user_a.id)
    sess_b = create_session(db_session, meeting.id, user_b.id)

    # Both clients observed the exact same utterance at roughly the same time
    dom_repo.bulk_insert_segments(meeting.id, sess_a.session_id, [
        {
            "sequence": 1,
            "speaker_name": "Boss",
            "text": "Dự án này cần hoàn thành trước thứ Sáu.",
            "observed_start_epoch_ms": 1700000000000,
            "observed_end_epoch_ms": 1700000003000,
            "confidence_score": 0.95,
        }
    ])
    dom_repo.bulk_insert_segments(meeting.id, sess_b.session_id, [
        {
            "sequence": 1,
            "speaker_name": "Boss",
            "text": "Dự án này cần hoàn thành trước thứ Sáu.",
            "observed_start_epoch_ms": 1700000000050,  # 50ms difference
            "observed_end_epoch_ms": 1700000003050,
            "confidence_score": 0.90,
        }
    ])

    result = agg_service.compute_aggregated_view(meeting.id)
    assert result.total_raw_segments == 2
    # Must be deduplicated into EXACTLY 1 clean entry
    assert result.total_aggregated_entries == 1
    assert result.entries[0].speaker == "Boss"
    assert result.entries[0].text == "Dự án này cần hoàn thành trước thứ Sáu."
    assert len(result.session_contributors) == 2


def test_m4_transcript_view_service_integration(db_session: Session) -> None:
    """Verify TranscriptViewService integrates DOMAggregationService as fallback."""
    user = create_user(db_session, "viewer@example.com")
    meeting_repo = MeetingRepository(db_session)
    dom_repo = MeetingDOMSegmentRepository(db_session)
    view_service = TranscriptViewService(db_session)

    meeting = meeting_repo.create({
        "user_id": user.id,
        "title": "M4 Transcript View Test",
        "status": "completed",
        "dom_capture_status": "received",
    })

    sess = create_session(db_session, meeting.id, user.id)
    dom_repo.bulk_insert_segments(meeting.id, sess.session_id, [
        {
            "sequence": 1,
            "speaker_name": "Presenter",
            "text": "Chào mừng các bạn đến với demo.",
            "observed_start_epoch_ms": 1700000000000,
            "observed_end_epoch_ms": 1700000002000,
        }
    ])

    response = view_service.get_transcript_view(meeting.id, user_id=user.id)
    assert response.source == "DOM"
    assert response.total_entries == 1
    assert response.entries[0].speaker == "Presenter"
    assert response.cross_session_coverage is not None
