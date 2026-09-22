import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Meeting, Participant, Transcript, TranscriptEntry, User
from app.services.transcript_token_optimizer import (
    DEFAULT_MAX_TRANSCRIPT_CHARS,
    TranscriptInputTooLargeError,
    TranscriptTokenOptimizer,
    estimate_token_count,
    optimize_transcript_text,
)


def test_none_input_returns_empty_result() -> None:
    result = optimize_transcript_text(None)
    assert result.optimized_text == ""
    assert result.character_count == 0
    assert result.word_count == 0
    assert result.estimated_token_count == 0
    assert result.exceeds_limit is False
    assert result.selected_source == "none"


def test_empty_string_returns_empty_result() -> None:
    result = optimize_transcript_text("")
    assert result.optimized_text == ""
    assert result.character_count == 0
    assert result.word_count == 0
    assert result.estimated_token_count == 0
    assert result.exceeds_limit is False


def test_single_transcript_line_remains_semantically_unchanged() -> None:
    line = "[09:00:00] Alice: Hello team."
    result = optimize_transcript_text(line, selected_source="official")
    assert result.optimized_text == line
    assert result.character_count == len(line)
    assert result.selected_source == "official"


def test_multiple_lines_ordered_and_newlines_normalized() -> None:
    raw = "[09:00:00] Alice: Line 1\r\n\r\n[09:00:05] Bob: Line 2\r\n"
    result = optimize_transcript_text(raw)
    expected = "[09:00:00] Alice: Line 1\n[09:00:05] Bob: Line 2"
    assert result.optimized_text == expected


def test_redundant_formatting_blank_lines_and_trailing_spaces() -> None:
    raw = "   \n[09:00:00] Alice: Hello   \n\n\n[09:00:05] Bob: World   \n   "
    result = optimize_transcript_text(raw)
    expected = "[09:00:00] Alice: Hello\n[09:00:05] Bob: World"
    assert result.optimized_text == expected


def test_meaningful_line_boundaries_speaker_and_timestamps_preserved() -> None:
    raw = "[09:00:00] Alice Specialist: Line 1\n[09:01:00] Bob Engineer: Line 2"
    result = optimize_transcript_text(raw)
    assert result.optimized_text == raw


def test_vietnamese_unicode_and_diacritics_preserved_100_percent() -> None:
    raw = (
        "[09:00:00] Nguyễn Văn A: Hôm nay chúng ta sẽ thảo luận về kế hoạch dự án.\n"
        "[09:00:05] Trần Thị B: Báo cáo tiến độ hoàn tất 100%."
    )
    result = optimize_transcript_text(raw)
    assert result.optimized_text == raw


def test_no_paraphrasing_translation_or_invented_words() -> None:
    raw = "[09:00:00] User: ko, không và & test."
    result = optimize_transcript_text(raw)
    assert result.optimized_text == raw


def test_transcript_under_budget_not_exceeding_limit() -> None:
    raw = "[09:00:00] Alice: Short text under budget."
    result = optimize_transcript_text(raw, max_chars=120_000)
    assert result.exceeds_limit is False
    assert result.character_count == len(raw)


def test_transcript_exactly_at_budget() -> None:
    # Construct text of exactly 100 chars
    base = "[09:00:00] Speaker: "
    padding = "A" * (100 - len(base))
    exact_text = base + padding
    assert len(exact_text) == 100

    result = optimize_transcript_text(exact_text, max_chars=100)
    assert result.character_count == 100
    assert result.exceeds_limit is False


def test_transcript_above_budget_flags_exceeds_limit() -> None:
    large_text = "A" * 101
    result = optimize_transcript_text(large_text, max_chars=100, raise_on_limit=False)
    assert result.exceeds_limit is True
    assert result.character_count == 101


def test_transcript_above_budget_raises_exception_when_requested() -> None:
    large_text = "A" * 101
    with pytest.raises(TranscriptInputTooLargeError) as exc_info:
        optimize_transcript_text(large_text, max_chars=100, raise_on_limit=True)

    err = exc_info.value
    assert err.error_code == "INPUT_TOO_LARGE"
    assert err.status_code == 422
    assert err.character_count == 101
    assert err.max_chars == 100


def test_estimate_token_count_calculation() -> None:
    assert estimate_token_count("") == 0
    assert estimate_token_count("1234") == 1
    assert estimate_token_count("12345") == 2
    assert estimate_token_count("A" * 100) == 25


def test_determinism_and_idempotency() -> None:
    raw = "[09:00:00] Alice: Hello\r\n\r\n[09:00:05] Bob: World  "
    res1 = optimize_transcript_text(raw)
    res2 = optimize_transcript_text(raw)
    res3 = optimize_transcript_text(res1.optimized_text)

    assert res1 == res2
    assert res1.optimized_text == res3.optimized_text


def test_transcript_token_optimizer_db_integration_read_only(
    db_session: Session,
) -> None:
    user = User(
        google_user_id="user-opt-01",
        email="opt@example.com",
        display_name="Optimizer User",
    )
    db_session.add(user)
    db_session.commit()

    meeting = Meeting(
        user_id=user.id,
        conference_record_name="conferenceRecords/conf-opt-test",
        title="Optimizer Test Meeting",
    )
    db_session.add(meeting)
    db_session.commit()

    part = Participant(
        meeting_id=meeting.id,
        google_participant_name="conferenceRecords/conf-opt-test/participants/p1",
        display_name="Alice Specialist",
    )
    db_session.add(part)
    db_session.commit()

    tx = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="conferenceRecords/conf-opt-test/transcripts/t1",
        state="FILE_GENERATED",
    )
    db_session.add(tx)
    db_session.commit()

    entry = TranscriptEntry(
        transcript_id=tx.id,
        participant_id=part.id,
        google_entry_name="conferenceRecords/conf-opt-test/transcripts/t1/entries/e1",
        text="Thảo luận dự án backend.\r\n\r\n",
        start_time=datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    optimizer = TranscriptTokenOptimizer(db_session)
    result = optimizer.optimize_by_meeting_id(meeting.id)

    assert result.selected_source == "official"
    assert result.optimized_text == "[09:00:00] Alice Specialist: Thảo luận dự án backend."
    assert result.exceeds_limit is False

    # Read-only verification: DB unchanged
    assert db_session.get(TranscriptEntry, entry.id).text == "Thảo luận dự án backend.\r\n\r\n"
