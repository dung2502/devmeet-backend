import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Meeting, User
from app.services.dom_transcript_normalizer import (
    DomTranscriptNormalizer,
    format_dom_timestamp,
    normalize_dom_transcript_data,
)


def test_format_dom_timestamp_various_inputs() -> None:
    dt = datetime(2026, 8, 21, 9, 2, 10, tzinfo=timezone.utc)
    assert format_dom_timestamp(dt) == "[09:02:10]"
    assert format_dom_timestamp("09:02:10") == "[09:02:10]"
    assert format_dom_timestamp("09:02:10.123") == "[09:02:10]"
    assert format_dom_timestamp("2026-08-21T09:02:10Z") == "[09:02:10]"
    assert format_dom_timestamp(130) == "[00:02:10]"
    assert format_dom_timestamp(3730) == "[01:02:10]"
    assert format_dom_timestamp(None) == "[00:00:00]"
    assert format_dom_timestamp("") == "[00:00:00]"
    assert format_dom_timestamp("invalid") == "[00:00:00]"


def test_empty_dom_transcript() -> None:
    assert normalize_dom_transcript_data(None) == ""
    assert normalize_dom_transcript_data({}) == ""
    assert normalize_dom_transcript_data([]) == ""
    assert normalize_dom_transcript_data({"entries": []}) == ""


def test_single_dom_transcript_entry() -> None:
    data = {
        "entries": [
            {
                "speaker": "Nguyễn Văn A",
                "text": "Xin chào mọi người.",
                "start_time": "00:00:03",
            }
        ]
    }
    result = normalize_dom_transcript_data(data)
    assert result == "[00:00:03] Nguyễn Văn A: Xin chào mọi người."


def test_multiple_entries_deterministic_ordering() -> None:
    # Passed out of order
    data = [
        {
            "speaker": "Trần Văn B",
            "text": "Chúng ta bắt đầu cuộc họp nhé.",
            "start_time": "00:00:08",
        },
        {
            "speaker": "Nguyễn Văn A",
            "text": "Xin chào mọi người.",
            "start_time": "00:00:03",
        },
    ]
    result = normalize_dom_transcript_data(data)
    expected = (
        "[00:00:03] Nguyễn Văn A: Xin chào mọi người.\n"
        "[00:00:08] Trần Văn B: Chúng ta bắt đầu cuộc họp nhé."
    )
    assert result == expected


def test_speaker_normalization_human_readable() -> None:
    data = {
        "entries": [
            {
                "display_name": "Alice Specialist",
                "text": "Testing display_name key.",
                "start_time": "09:00:00",
            }
        ]
    }
    result = normalize_dom_transcript_data(data)
    assert result == "[09:00:00] Alice Specialist: Testing display_name key."


def test_missing_speaker_falls_back_to_unknown() -> None:
    data = [
        {"text": "No speaker field provided.", "start_time": "09:00:00"},
        {"speaker": None, "text": "Null speaker.", "start_time": "09:01:00"},
        {"speaker": "   ", "text": "Whitespace speaker.", "start_time": "09:02:00"},
    ]
    result = normalize_dom_transcript_data(data)
    expected = (
        "[09:00:00] Unknown: No speaker field provided.\n"
        "[09:01:00] Unknown: Null speaker.\n"
        "[09:02:00] Unknown: Whitespace speaker."
    )
    assert result == expected


def test_missing_null_timestamp() -> None:
    data = [{"speaker": "User X", "text": "No start_time.", "start_time": None}]
    result = normalize_dom_transcript_data(data)
    assert result == "[00:00:00] User X: No start_time."


def test_missing_null_text_does_not_output_none() -> None:
    data = [{"speaker": "User X", "text": None, "start_time": "09:00:00"}]
    result = normalize_dom_transcript_data(data)
    assert "None" not in result
    assert result == "[09:00:00] User X: "


def test_empty_whitespace_text() -> None:
    data = [{"speaker": "User X", "text": "   ", "start_time": "09:00:00"}]
    result = normalize_dom_transcript_data(data)
    assert result == "[09:00:00] User X:    "


def test_vietnamese_unicode_text_preserved() -> None:
    data = {
        "entries": [
            {
                "speaker": "Lê Thị Cần",
                "text": "Báo cáo tiến độ dự án DevMeeting AI hoàn tất 100%.",
                "start_time": "10:15:30",
            }
        ]
    }
    result = normalize_dom_transcript_data(data)
    assert result == "[10:15:30] Lê Thị Cần: Báo cáo tiến độ dự án DevMeeting AI hoàn tất 100%."


def test_technical_identifiers_rejected_as_speaker() -> None:
    raw_uuid = str(uuid.uuid4())
    data = [
        {"speaker": raw_uuid, "text": "Message 1", "start_time": "09:00:00"},
        {"speaker": f"user_{raw_uuid}", "text": "Message 2", "start_time": "09:01:00"},
    ]
    result = normalize_dom_transcript_data(data)
    assert raw_uuid not in result
    expected = (
        "[09:00:00] Unknown: Message 1\n"
        "[09:01:00] Unknown: Message 2"
    )
    assert result == expected


def test_determinism_repeated_calls_same_output() -> None:
    data = {
        "entries": [
            {"speaker": "A", "text": "Hello", "start_time": "09:00:00"},
            {"speaker": "B", "text": "World", "start_time": "09:00:05"},
        ]
    }
    r1 = normalize_dom_transcript_data(data)
    r2 = normalize_dom_transcript_data(data)
    assert r1 == r2 == "[09:00:00] A: Hello\n[09:00:05] B: World"


def test_dom_transcript_normalizer_db_integration_read_only(
    db_session: Session,
) -> None:
    user = User(
        google_user_id="user-dom-01",
        email="dom@example.com",
        display_name="DOM User",
    )
    db_session.add(user)
    db_session.commit()

    dom_data = {
        "session_id": "sess-test-123",
        "entries": [
            {
                "speaker": "Nguyễn Văn A",
                "text": "Chúng ta bắt đầu họp Sprint 42 review.",
                "start_time": "09:00:15",
            },
            {
                "speaker": "Trần Thị B",
                "text": "Phần giao diện Extension đã làm xong.",
                "start_time": "09:02:10",
            },
        ],
    }

    meeting = Meeting(
        user_id=user.id,
        conference_record_name="conferenceRecords/conf-dom-test",
        title="DOM Integration Meeting",
        dom_capture_status="received",
        dom_transcript_data=dom_data,
    )
    db_session.add(meeting)
    db_session.commit()

    normalizer = DomTranscriptNormalizer(db_session)
    result = normalizer.normalize_by_meeting_id(meeting.id)

    expected = (
        "[09:00:15] Nguyễn Văn A: Chúng ta bắt đầu họp Sprint 42 review.\n"
        "[09:02:10] Trần Thị B: Phần giao diện Extension đã làm xong."
    )
    assert result == expected

    # Verify read-only behavior: meeting record unchanged
    fetched_meeting = db_session.get(Meeting, meeting.id)
    assert fetched_meeting.dom_capture_status == "received"
    assert fetched_meeting.dom_transcript_data == dom_data


def test_dom_transcript_normalizer_missing_meeting_or_data(
    db_session: Session,
) -> None:
    normalizer = DomTranscriptNormalizer(db_session)
    # Non-existent meeting ID -> ""
    assert normalizer.normalize_by_meeting_id(uuid.uuid4()) == ""
