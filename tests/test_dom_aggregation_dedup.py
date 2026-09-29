import difflib
import math
import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.models import LiveSession, LiveSessionStatus, Meeting, MeetingDOMSegment, User
from app.repositories import LiveSessionRepository, MeetingDOMSegmentRepository, MeetingRepository
from app.schemas.transcript_view import TranscriptViewEntry
from app.services.dom_aggregation_service import DOMAggregationService, strip_committed_prefix


# ─── Test 1: strip_committed_prefix Unit Tests ────────────────────────────────

def test_strip_committed_prefix_direct():
    res = strip_committed_prefix("Alo alo alo alo.", "Alo alo alo.")
    assert res == "alo."


def test_strip_committed_prefix_teams_sequence():
    seq1 = "Alo alo alo."
    seq2 = "Alo alo alo alo."
    seq3 = "Alo alo alo alo. 1 2 3 1 2 3 1 2 3"
    seq4 = "Alo alo alo alo. 1 2 3 1 2 3 1 2 3 Alo 1, 2 3."
    seq5 = "Alo alo alo alo. 1 2 3 1 2 3 1 2 3 Alo 1, 2 3. Nhưng mà nó lại có chia ra được cái tham minh đúng không"
    seq6 = "Alo alo alo alo. 1 2 3 1 2 3 1 2 3 Alo 1, 2 3. Nhưng mà nó lại có chia ra được cái tham minh đúng không nhưng mà cái nội dung nó lại bị lặp"

    assert strip_committed_prefix(seq2, seq1) == "alo."
    assert strip_committed_prefix(seq3, seq2) == "1 2 3 1 2 3 1 2 3"
    assert strip_committed_prefix(seq4, seq3) == "Alo 1, 2 3."
    assert strip_committed_prefix(seq5, seq4) == "Nhưng mà nó lại có chia ra được cái tham minh đúng không"
    assert strip_committed_prefix(seq6, seq5) == "nhưng mà cái nội dung nó lại bị lặp"


def test_strip_committed_prefix_edge_cases():
    # Word boundary guard: 'Alonso' should not be stripped by 'Alo'
    assert strip_committed_prefix("Alonso is driving", "Alo") == "Alonso is driving"

    # Distinct sentences sharing 1 word: 'Xin chào' vs 'Xin lỗi tôi đến muộn'
    assert strip_committed_prefix("Xin lỗi tôi đến muộn", "Xin chào") == "Xin lỗi tôi đến muộn"

    # Identical
    assert strip_committed_prefix("Xin chào các bạn", "Xin chào các bạn") == ""

    # Retraction / subset
    assert strip_committed_prefix("Xin chào", "Xin chào các bạn") == ""

    # None or empty
    assert strip_committed_prefix("Xin chào", None) == "Xin chào"
    assert strip_committed_prefix("Xin chào", "") == "Xin chào"


# ─── Test 2: Database End-to-End Aggregation Tests ────────────────────────────

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


def create_session(db: Session, meeting_id: uuid.UUID, user_id: uuid.UUID) -> LiveSession:
    sess = LiveSession(
        session_id=uuid.uuid4(),
        meeting_id=meeting_id,
        user_id=user_id,
        tab_session_uuid=uuid.uuid4(),
        status=LiveSessionStatus.COMPLETED.value,
        client_server_offset_ms=0,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def test_teams_cumulative_caption_aggregation(db_session: Session) -> None:
    """
    Test DOMAggregationService against real-world MS Teams cumulative captions.
    Verifies that:
    1. Sequences 1 to 4 (within 3s gap) are merged into one progressive entry without duplication.
    2. Sequence 5 (after 9.2s gap) strips the previous 20-word prefix.
    3. Sequence 6 (after 3.1s gap) strips the previous 60-word prefix.
    4. Raw database records in meeting_dom_segments remain completely untouched (Never Delete Raw).
    """
    user = create_user(db_session, "teams_user@example.com")
    meeting_repo = MeetingRepository(db_session)
    dom_repo = MeetingDOMSegmentRepository(db_session)
    agg_service = DOMAggregationService(db_session)

    meeting = meeting_repo.create({
        "user_id": user.id,
        "title": "MS Teams Cumulative Caption Test",
        "start_time": datetime(2026, 9, 29, 10, 0, tzinfo=UTC),
        "status": "completed",
    })

    sess = create_session(db_session, meeting.id, user.id)

    # Exact 6 cumulative segments from user's report
    raw_segments = [
        {
            "sequence": 1,
            "speaker_name": "Giang Trần Hoàng",
            "text": "Alo alo alo.",
            "observed_start_epoch_ms": 1790670414734,
            "observed_end_epoch_ms": 1790670416734,
        },
        {
            "sequence": 2,
            "speaker_name": "Giang Trần Hoàng",
            "text": "Alo alo alo alo.",
            "observed_start_epoch_ms": 1790670416960,
            "observed_end_epoch_ms": 1790670418960,
        },
        {
            "sequence": 3,
            "speaker_name": "Giang Trần Hoàng",
            "text": "Alo alo alo alo. 1 2 3 1 2 3 1 2 3",
            "observed_start_epoch_ms": 1790670420792,
            "observed_end_epoch_ms": 1790670422792,
        },
        {
            "sequence": 4,
            "speaker_name": "Giang Trần Hoàng",
            "text": "Alo alo alo alo. 1 2 3 1 2 3 1 2 3 Alo 1, 2 3.",
            "observed_start_epoch_ms": 1790670423192,
            "observed_end_epoch_ms": 1790670425192,
        },
        {
            "sequence": 5,
            "speaker_name": "Giang Trần Hoàng",
            "text": "Alo alo alo alo. 1 2 3 1 2 3 1 2 3 Alo 1, 2 3. Nhưng mà nó lại có chia ra được cái tham minh đúng không Bên này nó không bắt được cái thăm mình nó không bắt được nhiều aft anh anh anh anh bắt đầu",
            "observed_start_epoch_ms": 1790670434407,
            "observed_end_epoch_ms": 1790670436407,
        },
        {
            "sequence": 6,
            "speaker_name": "Giang Trần Hoàng",
            "text": "Alo alo alo alo. 1 2 3 1 2 3 1 2 3 Alo 1, 2 3. Nhưng mà nó lại có chia ra được cái tham minh đúng không Bên này nó không bắt được cái thăm mình nó không bắt được nhiều aft anh anh anh anh bắt đầu nhưng mà cái nội dung nó lại bị lặp Đúng không Nó công chui vô.",
            "observed_start_epoch_ms": 1790670439519,
            "observed_end_epoch_ms": 1790670441519,
        },
    ]

    dom_repo.bulk_insert_segments(meeting.id, sess.session_id, raw_segments)

    # 1. Verify Raw Invariant: All 6 raw segments exist in meeting_dom_segments
    stored_raw = dom_repo.list_by_meeting_id(meeting.id)
    assert len(stored_raw) == 6

    # 2. Compute Derived View
    result = agg_service.compute_aggregated_view(meeting.id)

    # 3. Assert View has 3 clean non-repeating entries
    assert result.total_raw_segments == 6
    assert result.total_aggregated_entries == 3

    # Entry 1: Progressive merge of Seq 1-4
    assert result.entries[0].speaker == "Giang Trần Hoàng"
    assert result.entries[0].text == "Alo alo alo alo. 1 2 3 1 2 3 1 2 3 Alo 1, 2 3."

    # Entry 2: Seq 5 with previous 20 words stripped
    assert result.entries[1].speaker == "Giang Trần Hoàng"
    assert result.entries[1].text == "Nhưng mà nó lại có chia ra được cái tham minh đúng không Bên này nó không bắt được cái thăm mình nó không bắt được nhiều aft anh anh anh anh bắt đầu"
    assert "Alo alo" not in result.entries[1].text
    assert "1 2 3" not in result.entries[1].text

    # Entry 3: Seq 6 with previous 60 words stripped
    assert result.entries[2].speaker == "Giang Trần Hoàng"
    assert result.entries[2].text == "nhưng mà cái nội dung nó lại bị lặp Đúng không Nó công chui vô."
    assert "Alo alo" not in result.entries[2].text
    assert "tham minh" not in result.entries[2].text

    # 4. Re-verify Raw Invariant: raw table still has 6 segments intact!
    stored_raw_after = dom_repo.list_by_meeting_id(meeting.id)
    assert len(stored_raw_after) == 6
    assert stored_raw_after[5].text.startswith("Alo alo alo alo.")


def test_google_meet_independent_turns_not_stripped(db_session: Session) -> None:
    """
    Verify that Google Meet independent turns (with distinct sentences)
    are preserved 100% without any accidental stripping.
    """
    user = create_user(db_session, "meet_user@example.com")
    meeting_repo = MeetingRepository(db_session)
    dom_repo = MeetingDOMSegmentRepository(db_session)
    agg_service = DOMAggregationService(db_session)

    meeting = meeting_repo.create({
        "user_id": user.id,
        "title": "Google Meet Invariant Test",
        "status": "completed",
    })

    sess = create_session(db_session, meeting.id, user.id)

    raw_segments = [
        {
            "sequence": 1,
            "speaker_name": "Speaker A",
            "text": "Xin chào cả nhà.",
            "observed_start_epoch_ms": 1700000000000,
            "observed_end_epoch_ms": 1700000002000,
        },
        {
            "sequence": 2,
            "speaker_name": "Speaker A",
            "text": "Chúng ta bắt đầu họp về sprint mới nhé.",
            "observed_start_epoch_ms": 1700000008000,  # 6s gap > 3s
            "observed_end_epoch_ms": 1700000012000,
        },
    ]

    dom_repo.bulk_insert_segments(meeting.id, sess.session_id, raw_segments)

    result = agg_service.compute_aggregated_view(meeting.id)
    assert result.total_raw_segments == 2
    assert result.total_aggregated_entries == 2
    assert result.entries[0].text == "Xin chào cả nhà."
    assert result.entries[1].text == "Chúng ta bắt đầu họp về sprint mới nhé."
