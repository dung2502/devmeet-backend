import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Meeting, Participant, Transcript, TranscriptEntry, User
from app.services.transcript_source_selector import (
    TranscriptSourceSelector,
    select_transcript_source,
)


def test_select_official_transcript_when_only_official_exists() -> None:
    official = "[09:00:00] Alice: Hello"
    result = select_transcript_source(official_text=official, dom_text=None)
    assert result.selected_source == "official"
    assert result.transcript_text == official


def test_select_dom_transcript_when_official_is_empty() -> None:
    dom = "[00:00:05] Bob: Hi there"
    result = select_transcript_source(official_text="", dom_text=dom)
    assert result.selected_source == "dom"
    assert result.transcript_text == dom


def test_select_dom_transcript_when_official_is_none() -> None:
    dom = "[00:00:05] Bob: Hi there"
    result = select_transcript_source(official_text=None, dom_text=dom)
    assert result.selected_source == "dom"
    assert result.transcript_text == dom


def test_both_sources_exist_respects_official_precedence() -> None:
    official = "[09:00:00] Alice: Official message"
    dom = "[09:00:00] Alice: DOM message"
    result = select_transcript_source(official_text=official, dom_text=dom)
    assert result.selected_source == "official"
    assert result.transcript_text == official


def test_both_sources_empty_returns_none() -> None:
    result = select_transcript_source(official_text="", dom_text="")
    assert result.selected_source == "none"
    assert result.transcript_text == ""


def test_both_sources_none_returns_none() -> None:
    result = select_transcript_source(official_text=None, dom_text=None)
    assert result.selected_source == "none"
    assert result.transcript_text == ""


def test_official_contains_only_whitespace_falls_back_to_dom() -> None:
    official_whitespace = "   \n\t  "
    dom = "[00:00:05] Bob: Fallback from whitespace official"
    result = select_transcript_source(official_text=official_whitespace, dom_text=dom)
    assert result.selected_source == "dom"
    assert result.transcript_text == dom


def test_dom_contains_only_whitespace_returns_none_when_official_empty() -> None:
    dom_whitespace = "   \n\t  "
    result = select_transcript_source(official_text="", dom_text=dom_whitespace)
    assert result.selected_source == "none"
    assert result.transcript_text == ""


def test_selected_transcript_returned_unchanged() -> None:
    raw_official = "[09:00:00] Multi-line User: Line 1\n[09:00:05] Multi-line User: Line 2"
    result = select_transcript_source(official_text=raw_official, dom_text=None)
    assert result.transcript_text == raw_official


def test_repeated_selection_with_identical_inputs_is_deterministic() -> None:
    off = "[09:00:00] Alice: Deterministic test"
    dom = "[09:00:00] Alice: Fallback test"
    r1 = select_transcript_source(off, dom)
    r2 = select_transcript_source(off, dom)
    assert r1 == r2
    assert r1.selected_source == "official"
    assert r1.transcript_text == off


def test_transcript_source_selector_db_integration_read_only(
    db_session: Session,
) -> None:
    user = User(
        google_user_id="user-sel-01",
        email="sel@example.com",
        display_name="Selector User",
    )
    db_session.add(user)
    db_session.commit()

    dom_data = {
        "entries": [
            {"speaker": "Bob DOM", "text": "DOM transcript fallback line.", "start_time": "09:00:00"}
        ]
    }
    meeting = Meeting(
        user_id=user.id,
        conference_record_name="conferenceRecords/conf-sel-test",
        title="Selector Test Meeting",
        dom_capture_status="received",
        dom_transcript_data=dom_data,
    )
    db_session.add(meeting)
    db_session.commit()

    # Case A: Official transcript not ready / missing -> DOM selected
    selector = TranscriptSourceSelector(db_session)
    res_dom = selector.select_source_by_meeting_id(meeting.id)
    assert res_dom.selected_source == "dom"
    assert res_dom.transcript_text == "[09:00:00] Bob DOM: DOM transcript fallback line."

    # Case B: Add Official Transcript -> Official selected
    part = Participant(
        meeting_id=meeting.id,
        google_participant_name="conferenceRecords/conf-sel-test/participants/p1",
        display_name="Alice Official",
    )
    db_session.add(part)
    db_session.commit()

    tx = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="conferenceRecords/conf-sel-test/transcripts/t1",
        state="FILE_GENERATED",
    )
    db_session.add(tx)
    db_session.commit()

    entry = TranscriptEntry(
        transcript_id=tx.id,
        participant_id=part.id,
        google_entry_name="conferenceRecords/conf-sel-test/transcripts/t1/entries/e1",
        text="Official transcript line.",
        start_time=datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    res_official = selector.select_source_by_meeting_id(meeting.id)
    assert res_official.selected_source == "official"
    assert res_official.transcript_text == "[09:00:00] Alice Official: Official transcript line."

    # Verify Database Read-Only Safety
    fetched_meeting = db_session.get(Meeting, meeting.id)
    assert fetched_meeting.dom_capture_status == "received"
    assert db_session.get(TranscriptEntry, entry.id).text == "Official transcript line."
