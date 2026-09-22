import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Meeting, Participant, Transcript, TranscriptEntry, User
from app.services.transcript_normalizer import (
    OfficialTranscriptNormalizer,
    format_timestamp,
    normalize_official_transcript_entries,
)


def test_format_timestamp_valid_and_none() -> None:
    dt = datetime(2026, 8, 21, 9, 15, 10, tzinfo=timezone.utc)
    assert format_timestamp(dt) == "[09:15:10]"
    assert format_timestamp(None) == "[00:00:00]"


def test_normalize_empty_entries() -> None:
    result = normalize_official_transcript_entries([])
    assert result == ""


def test_normalize_single_entry() -> None:
    entry = TranscriptEntry(
        id=uuid.uuid4(),
        transcript_id=uuid.uuid4(),
        google_entry_name="entries/e1",
        text="Xin chào cả đội.",
        start_time=datetime(2026, 8, 21, 9, 0, 15, tzinfo=timezone.utc),
    )
    result = normalize_official_transcript_entries([entry], default_speaker="Unknown")
    assert result == "[09:00:15] Unknown: Xin chào cả đội."


def test_normalize_multiple_entries_sorting_and_participant_mapping() -> None:
    p1_id = uuid.uuid4()
    p2_id = uuid.uuid4()
    participants_map = {
        p1_id: "Nguyen Van A",
        p2_id: "Tran Thi B",
    }

    # Pass in unsorted order
    e2 = TranscriptEntry(
        id=uuid.uuid4(),
        transcript_id=uuid.uuid4(),
        participant_id=p2_id,
        google_entry_name="entries/e2",
        text="Tôi đã hoàn thành giao diện.",
        start_time=datetime(2026, 8, 21, 9, 2, 0, tzinfo=timezone.utc),
    )
    e1 = TranscriptEntry(
        id=uuid.uuid4(),
        transcript_id=uuid.uuid4(),
        participant_id=p1_id,
        google_entry_name="entries/e1",
        text="Hôm nay tôi sẽ hoàn thành API đăng nhập.",
        start_time=datetime(2026, 8, 21, 9, 1, 15, tzinfo=timezone.utc),
    )

    result = normalize_official_transcript_entries([e2, e1], participants_map=participants_map)
    expected = (
        "[09:01:15] Nguyen Van A: Hôm nay tôi sẽ hoàn thành API đăng nhập.\n"
        "[09:02:00] Tran Thi B: Tôi đã hoàn thành giao diện."
    )
    assert result == expected


def test_normalize_null_participant_id_fallback_to_unknown() -> None:
    entry = TranscriptEntry(
        id=uuid.uuid4(),
        transcript_id=uuid.uuid4(),
        participant_id=None,
        google_entry_name="entries/e1",
        text="Speaker unidentified message.",
        start_time=datetime(2026, 8, 21, 10, 0, 0, tzinfo=timezone.utc),
    )
    result = normalize_official_transcript_entries([entry])
    assert result == "[10:00:00] Unknown: Speaker unidentified message."


def test_normalize_missing_timestamp_and_empty_text() -> None:
    entry = TranscriptEntry(
        id=uuid.uuid4(),
        transcript_id=uuid.uuid4(),
        participant_id=None,
        google_entry_name="entries/e1",
        text="",
        start_time=None,
    )
    result = normalize_official_transcript_entries([entry])
    assert result == "[00:00:00] Unknown: "


def test_normalize_repeated_calls_are_deterministic() -> None:
    entry = TranscriptEntry(
        id=uuid.uuid4(),
        transcript_id=uuid.uuid4(),
        google_entry_name="entries/e1",
        text="Test text.",
        start_time=datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc),
    )
    res1 = normalize_official_transcript_entries([entry])
    res2 = normalize_official_transcript_entries([entry])
    assert res1 == res2 == "[09:00:00] Unknown: Test text."


def test_normalize_does_not_contain_technical_ids() -> None:
    p_id = uuid.uuid4()
    tx_id = uuid.uuid4()
    entry = TranscriptEntry(
        id=uuid.uuid4(),
        transcript_id=tx_id,
        participant_id=p_id,
        google_entry_name="conferenceRecords/123/transcripts/abc/entries/xyz789",
        text="Clean output without UUIDs.",
        start_time=datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc),
    )
    result = normalize_official_transcript_entries(
        [entry], participants_map={p_id: "Dev User"}
    )
    assert str(p_id) not in result
    assert str(tx_id) not in result
    assert "conferenceRecords" not in result
    assert result == "[09:00:00] Dev User: Clean output without UUIDs."


def test_official_transcript_normalizer_service_db_integration(
    db_session: Session,
) -> None:
    # Set up DB records
    user = User(
        google_user_id="user-norm-01",
        email="norm@example.com",
        display_name="Norm User",
    )
    db_session.add(user)
    db_session.commit()

    meeting = Meeting(
        user_id=user.id,
        conference_record_name="conferenceRecords/conf-norm-test",
        title="Normalization Test Meeting",
    )
    db_session.add(meeting)
    db_session.commit()

    part1 = Participant(
        meeting_id=meeting.id,
        google_participant_name="conferenceRecords/conf-norm-test/participants/p1",
        display_name="Alice Specialist",
    )
    part2 = Participant(
        meeting_id=meeting.id,
        google_participant_name="conferenceRecords/conf-norm-test/participants/p2",
        display_name="Bob Engineer",
    )
    db_session.add_all([part1, part2])
    db_session.commit()

    transcript = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="conferenceRecords/conf-norm-test/transcripts/t1",
        state="FILE_GENERATED",
    )
    db_session.add(transcript)
    db_session.commit()

    e1 = TranscriptEntry(
        transcript_id=transcript.id,
        participant_id=part1.id,
        google_entry_name="conferenceRecords/conf-norm-test/transcripts/t1/entries/e1",
        text="Thảo luận phần Backend API.",
        start_time=datetime(2026, 8, 21, 14, 0, 10, tzinfo=timezone.utc),
    )
    e2 = TranscriptEntry(
        transcript_id=transcript.id,
        participant_id=part2.id,
        google_entry_name="conferenceRecords/conf-norm-test/transcripts/t1/entries/e2",
        text="Tôi đồng ý với giải pháp.",
        start_time=datetime(2026, 8, 21, 14, 1, 5, tzinfo=timezone.utc),
    )
    db_session.add_all([e1, e2])
    db_session.commit()

    # Instantiate normalizer service
    normalizer = OfficialTranscriptNormalizer(db_session)

    # Test normalize_by_meeting_id
    text_by_meeting = normalizer.normalize_by_meeting_id(meeting.id)
    expected_text = (
        "[14:00:10] Alice Specialist: Thảo luận phần Backend API.\n"
        "[14:01:05] Bob Engineer: Tôi đồng ý với giải pháp."
    )
    assert text_by_meeting == expected_text

    # Test normalize_by_transcript_id
    text_by_transcript = normalizer.normalize_by_transcript_id(transcript.id)
    assert text_by_transcript == expected_text

    # Verify no mutation of DB records
    fetched_e1 = db_session.get(TranscriptEntry, e1.id)
    assert fetched_e1.text == "Thảo luận phần Backend API."
    fetched_part1 = db_session.get(Participant, part1.id)
    assert fetched_part1.display_name == "Alice Specialist"
