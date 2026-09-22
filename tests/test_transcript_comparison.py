import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Meeting, Participant, Transcript, TranscriptEntry, User
from app.services.transcript_comparison import (
    TranscriptComparisonEngine,
    compare_normalized_transcripts,
    parse_normalized_line,
)


def test_parse_normalized_line_valid_and_fallbacks() -> None:
    line = "[09:15:10] Alice Specialist: Hello team."
    parsed = parse_normalized_line(line)
    assert parsed is not None
    assert parsed.time_sec == 9 * 3600 + 15 * 60 + 10
    assert parsed.speaker == "Alice Specialist"
    assert parsed.text == "Hello team."

    # Empty line
    assert parse_normalized_line("   ") is None


def test_compare_both_transcripts_empty_or_missing() -> None:
    res1 = compare_normalized_transcripts(None, None)
    assert res1.comparison_status == "skipped"
    assert res1.metrics is None

    res2 = compare_normalized_transcripts("", "   ")
    assert res2.comparison_status == "skipped"
    assert res2.metrics is None


def test_compare_one_transcript_missing() -> None:
    res1 = compare_normalized_transcripts("[09:00:00] Alice: Text", None)
    assert res1.comparison_status == "skipped"
    assert res1.metrics is None

    res2 = compare_normalized_transcripts(None, "[09:00:00] Bob: Text")
    assert res2.comparison_status == "skipped"
    assert res2.metrics is None


def test_compare_identical_transcripts() -> None:
    official = "[09:00:00] Alice: Hello\n[09:00:05] Bob: World"
    dom = "[09:00:00] Alice: Hello\n[09:00:05] Bob: World"

    res = compare_normalized_transcripts(official, dom)
    assert res.comparison_status == "completed"
    assert res.metrics is not None
    assert res.metrics.coverage_rate == 1.0
    assert res.metrics.missing_count == 0
    assert res.metrics.duplicate_count == 0
    assert res.metrics.speaker_match_rate == 1.0
    assert res.metrics.timestamp_delta_avg_sec == 0.0


def test_compare_whitespace_and_newline_variations() -> None:
    official = "[09:00:00] Alice: Hello\r\n[09:00:05] Bob: World\r\n"
    dom = "\n[09:00:00] Alice: Hello   \n[09:00:05] Bob: World\n"

    res = compare_normalized_transcripts(official, dom)
    assert res.comparison_status == "completed"
    assert res.metrics is not None
    assert res.metrics.coverage_rate == 1.0
    assert res.metrics.missing_count == 0


def test_compare_missing_entries_in_dom() -> None:
    official = (
        "[09:00:00] Alice: Statement 1\n"
        "[09:01:00] Bob: Statement 2\n"
        "[09:02:00] Charlie: Statement 3"
    )
    dom = "[09:00:00] Alice: Statement 1"

    res = compare_normalized_transcripts(official, dom)
    assert res.comparison_status == "completed"
    assert res.metrics is not None
    assert res.metrics.coverage_rate == 0.333
    assert res.metrics.missing_count == 2


def test_compare_duplicate_entries_in_dom() -> None:
    official = "[09:00:00] Alice: Statement 1"
    dom = (
        "[09:00:00] Alice: Statement 1\n"
        "[09:00:01] Alice: Statement 1"
    )

    res = compare_normalized_transcripts(official, dom)
    assert res.comparison_status == "completed"
    assert res.metrics is not None
    assert res.metrics.duplicate_count == 1


def test_compare_different_speaker_and_timestamp_deltas() -> None:
    official = "[09:00:00] Alice: We will release Friday."
    dom = "[09:00:05] Unknown: We will release Friday."

    res = compare_normalized_transcripts(official, dom)
    assert res.comparison_status == "completed"
    assert res.metrics is not None
    assert res.metrics.coverage_rate == 1.0
    assert res.metrics.speaker_match_rate == 0.0
    assert res.metrics.timestamp_delta_avg_sec == 5.0


def test_compare_vietnamese_unicode_text() -> None:
    official = "[09:00:00] Nguyễn Văn A: Báo cáo tiến độ cuộc họp."
    dom = "[09:00:00] Nguyễn Văn A: Báo cáo tiến độ cuộc họp."

    res = compare_normalized_transcripts(official, dom)
    assert res.comparison_status == "completed"
    assert res.metrics is not None
    assert res.metrics.coverage_rate == 1.0


def test_compare_repeated_calls_are_deterministic() -> None:
    off = "[09:00:00] Alice: Test"
    dom = "[09:00:00] Alice: Test"
    r1 = compare_normalized_transcripts(off, dom)
    r2 = compare_normalized_transcripts(off, dom)
    assert r1 == r2


def test_comparison_engine_db_integration_read_only_by_default(
    db_session: Session,
) -> None:
    user = User(
        google_user_id="user-comp-01",
        email="comp@example.com",
        display_name="Comparison User",
    )
    db_session.add(user)
    db_session.commit()

    dom_data = {
        "entries": [
            {
                "speaker": "Alice Specialist",
                "text": "Meeting comparison text.",
                "start_time": "09:00:05",
            }
        ]
    }
    meeting = Meeting(
        user_id=user.id,
        conference_record_name="conferenceRecords/conf-comp-test",
        title="Comparison Test Meeting",
        dom_capture_status="received",
        dom_transcript_data=dom_data,
        comparison_status="pending",
    )
    db_session.add(meeting)
    db_session.commit()

    part = Participant(
        meeting_id=meeting.id,
        google_participant_name="conferenceRecords/conf-comp-test/participants/p1",
        display_name="Alice Specialist",
    )
    db_session.add(part)
    db_session.commit()

    tx = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="conferenceRecords/conf-comp-test/transcripts/t1",
        state="FILE_GENERATED",
    )
    db_session.add(tx)
    db_session.commit()

    entry = TranscriptEntry(
        transcript_id=tx.id,
        participant_id=part.id,
        google_entry_name="conferenceRecords/conf-comp-test/transcripts/t1/entries/e1",
        text="Meeting comparison text.",
        start_time=datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    engine = TranscriptComparisonEngine(db_session)

    # 1. Test read-only by default (persist=False)
    res_readonly = engine.compare_by_meeting_id(meeting.id, persist=False)
    assert res_readonly.comparison_status == "completed"
    assert res_readonly.metrics is not None
    assert res_readonly.metrics.coverage_rate == 1.0
    assert res_readonly.metrics.timestamp_delta_avg_sec == 5.0

    # DB record remains unchanged
    fetched_m = db_session.get(Meeting, meeting.id)
    assert fetched_m.comparison_status == "pending"
    assert fetched_m.comparison_metrics is None

    # 2. Test explicit persistence (persist=True)
    res_persisted = engine.compare_by_meeting_id(meeting.id, persist=True)
    assert res_persisted.comparison_status == "completed"

    # DB record updated
    fetched_m2 = db_session.get(Meeting, meeting.id)
    assert fetched_m2.comparison_status == "completed"
    assert fetched_m2.comparison_metrics is not None
    assert fetched_m2.comparison_metrics["coverage_rate"] == 1.0
    assert fetched_m2.comparison_metrics["timestamp_delta_avg_sec"] == 5.0
