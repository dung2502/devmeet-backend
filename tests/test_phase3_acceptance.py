import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Meeting, Participant, Transcript, TranscriptEntry, User
from app.services.dom_transcript_normalizer import DomTranscriptNormalizer
from app.services.transcript_comparison import TranscriptComparisonEngine
from app.services.transcript_normalizer import OfficialTranscriptNormalizer
from app.services.transcript_source_selector import TranscriptSourceSelector
from app.services.transcript_token_optimizer import (
    TranscriptInputTooLargeError,
    TranscriptTokenOptimizer,
)


@pytest.fixture()
def sample_user(db_session: Session) -> User:
    user = User(
        google_user_id="user-phase3-acc-01",
        email="acc@example.com",
        display_name="Acceptance User",
    )
    db_session.add(user)
    db_session.commit()
    return user


def test_scenario_a_official_transcript_only(
    db_session: Session, sample_user: User
) -> None:
    """Scenario A: Official transcript exists, no DOM transcript. Official selected and optimized."""
    meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-phase3-scen-a",
        title="Scenario A Meeting",
    )
    db_session.add(meeting)
    db_session.commit()

    part = Participant(
        meeting_id=meeting.id,
        google_participant_name="conferenceRecords/conf-phase3-scen-a/participants/p1",
        display_name="Alice Official",
    )
    db_session.add(part)
    db_session.commit()

    tx = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="conferenceRecords/conf-phase3-scen-a/transcripts/t1",
        state="FILE_GENERATED",
    )
    db_session.add(tx)
    db_session.commit()

    entry = TranscriptEntry(
        transcript_id=tx.id,
        participant_id=part.id,
        google_entry_name="conferenceRecords/conf-phase3-scen-a/transcripts/t1/entries/e1",
        text="Discussing official release schedule.",
        start_time=datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    # Step 3C
    selector = TranscriptSourceSelector(db_session)
    selection_res = selector.select_source_by_meeting_id(meeting.id)
    assert selection_res.selected_source == "official"
    assert "[09:00:00] Alice Official: Discussing official release schedule." in selection_res.transcript_text

    # Step 3D
    optimizer = TranscriptTokenOptimizer(db_session)
    opt_res = optimizer.optimize_by_meeting_id(meeting.id)
    assert opt_res.selected_source == "official"
    assert opt_res.optimized_text == "[09:00:00] Alice Official: Discussing official release schedule."
    assert opt_res.exceeds_limit is False

    # Step 3E (Comparison skipped because DOM is absent)
    comp_engine = TranscriptComparisonEngine(db_session)
    comp_res = comp_engine.compare_by_meeting_id(meeting.id)
    assert comp_res.comparison_status == "skipped"
    assert comp_res.metrics is None


def test_scenario_b_dom_fallback_only_free_google_account(
    db_session: Session, sample_user: User
) -> None:
    """Scenario B: No Official Transcript (Free Google Account), valid DOM Transcript available."""
    dom_data = {
        "session_id": "sess-scen-b-123",
        "entries": [
            {
                "speaker": "Bob DOM",
                "text": "Fallback transcript for personal account.",
                "start_time": "09:00:05",
            }
        ],
    }
    meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-phase3-scen-b",
        title="Scenario B Free Account Meeting",
        dom_capture_status="received",
        dom_transcript_data=dom_data,
    )
    db_session.add(meeting)
    db_session.commit()

    # Step 3C: Selects DOM
    selector = TranscriptSourceSelector(db_session)
    selection_res = selector.select_source_by_meeting_id(meeting.id)
    assert selection_res.selected_source == "dom"

    # Step 3D: Token Optimizer
    optimizer = TranscriptTokenOptimizer(db_session)
    opt_res = optimizer.optimize_by_meeting_id(meeting.id)
    assert opt_res.selected_source == "dom"
    assert opt_res.optimized_text == "[09:00:05] Bob DOM: Fallback transcript for personal account."

    # Verify Rule 5 & 6: No fake Official Transcript records created
    tx_count = db_session.scalar(
        select(Transcript).where(Transcript.meeting_id == meeting.id)
    )
    assert tx_count is None
    entry_count = db_session.scalars(select(TranscriptEntry)).all()
    assert not any(e.google_entry_name.startswith("conferenceRecords/conf-phase3-scen-b") for e in entry_count)


def test_scenario_c_both_official_and_dom_exist(
    db_session: Session, sample_user: User
) -> None:
    """Scenario C: Both Official and DOM transcripts exist. Official selected, DOM independent, comparison executed."""
    dom_data = {
        "entries": [
            {
                "speaker": "Alice",
                "text": "Shared statement.",
                "start_time": "09:00:02",
            }
        ]
    }
    meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-phase3-scen-c",
        title="Scenario C Both Sources Meeting",
        dom_capture_status="received",
        dom_transcript_data=dom_data,
    )
    db_session.add(meeting)
    db_session.commit()

    part = Participant(
        meeting_id=meeting.id,
        google_participant_name="conferenceRecords/conf-phase3-scen-c/participants/p1",
        display_name="Alice",
    )
    db_session.add(part)
    db_session.commit()

    tx = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="conferenceRecords/conf-phase3-scen-c/transcripts/t1",
        state="FILE_GENERATED",
    )
    db_session.add(tx)
    db_session.commit()

    entry = TranscriptEntry(
        transcript_id=tx.id,
        participant_id=part.id,
        google_entry_name="conferenceRecords/conf-phase3-scen-c/transcripts/t1/entries/e1",
        text="Shared statement.",
        start_time=datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    # 1. Step 3C selects official
    selector = TranscriptSourceSelector(db_session)
    selection_res = selector.select_source_by_meeting_id(meeting.id)
    assert selection_res.selected_source == "official"

    # 2. Step 3D optimizes official
    optimizer = TranscriptTokenOptimizer(db_session)
    opt_res = optimizer.optimize_by_meeting_id(meeting.id)
    assert opt_res.selected_source == "official"
    assert opt_res.optimized_text == "[09:00:00] Alice: Shared statement."

    # 3. Step 3E runs comparison
    comp_engine = TranscriptComparisonEngine(db_session)
    comp_res = comp_engine.compare_by_meeting_id(meeting.id, persist=True)
    assert comp_res.comparison_status == "completed"
    assert comp_res.metrics is not None
    assert comp_res.metrics.coverage_rate == 1.0
    assert comp_res.metrics.timestamp_delta_avg_sec == 2.0

    # Verify DOM transcript data is untouched in DB
    fetched_m = db_session.get(Meeting, meeting.id)
    assert fetched_m.dom_transcript_data == dom_data
    assert fetched_m.comparison_status == "completed"
    assert fetched_m.comparison_metrics["coverage_rate"] == 1.0


def test_scenario_d_neither_source_available(
    db_session: Session, sample_user: User
) -> None:
    """Scenario D: Neither Official nor DOM transcript available."""
    meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-phase3-scen-d",
        title="Empty Meeting",
    )
    db_session.add(meeting)
    db_session.commit()

    selector = TranscriptSourceSelector(db_session)
    selection_res = selector.select_source_by_meeting_id(meeting.id)
    assert selection_res.selected_source == "none"

    optimizer = TranscriptTokenOptimizer(db_session)
    opt_res = optimizer.optimize_by_meeting_id(meeting.id)
    assert opt_res.selected_source == "none"
    assert opt_res.optimized_text == ""

    comp_engine = TranscriptComparisonEngine(db_session)
    comp_res = comp_engine.compare_by_meeting_id(meeting.id)
    assert comp_res.comparison_status == "skipped"


def test_scenario_e_official_transcript_above_120k_characters(
    db_session: Session, sample_user: User
) -> None:
    """Scenario E: Official transcript exceeds 120,000 character limit."""
    meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-phase3-scen-e",
        title="Large Official Meeting",
    )
    db_session.add(meeting)
    db_session.commit()

    part = Participant(
        meeting_id=meeting.id,
        google_participant_name="conferenceRecords/conf-phase3-scen-e/participants/p1",
        display_name="Alice Speaker",
    )
    db_session.add(part)
    db_session.commit()

    tx = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="conferenceRecords/conf-phase3-scen-e/transcripts/t1",
        state="FILE_GENERATED",
    )
    db_session.add(tx)
    db_session.commit()

    # Generate entry with text exceeding 120,000 characters
    large_text = "A" * 125_000
    entry = TranscriptEntry(
        transcript_id=tx.id,
        participant_id=part.id,
        google_entry_name="conferenceRecords/conf-phase3-scen-e/transcripts/t1/entries/e1",
        text=large_text,
        start_time=datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    selector = TranscriptSourceSelector(db_session)
    selection_res = selector.select_source_by_meeting_id(meeting.id)
    assert selection_res.selected_source == "official"

    optimizer = TranscriptTokenOptimizer(db_session)
    opt_res = optimizer.optimize_by_meeting_id(meeting.id, raise_on_limit=False)
    assert opt_res.exceeds_limit is True
    assert opt_res.character_count > 120_000

    with pytest.raises(TranscriptInputTooLargeError) as exc_info:
        optimizer.optimize_by_meeting_id(meeting.id, raise_on_limit=True)
    assert exc_info.value.error_code == "INPUT_TOO_LARGE"
    assert exc_info.value.status_code == 422


def test_scenario_f_dom_fallback_above_120k_characters(
    db_session: Session, sample_user: User
) -> None:
    """Scenario F: DOM fallback transcript exceeds 120,000 character limit."""
    large_dom_text = "B" * 125_000
    dom_data = {
        "entries": [
            {
                "speaker": "Bob Speaker",
                "text": large_dom_text,
                "start_time": "09:00:00",
            }
        ]
    }
    meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-phase3-scen-f",
        title="Large DOM Meeting",
        dom_capture_status="received",
        dom_transcript_data=dom_data,
    )
    db_session.add(meeting)
    db_session.commit()

    selector = TranscriptSourceSelector(db_session)
    selection_res = selector.select_source_by_meeting_id(meeting.id)
    assert selection_res.selected_source == "dom"

    optimizer = TranscriptTokenOptimizer(db_session)
    opt_res = optimizer.optimize_by_meeting_id(meeting.id, raise_on_limit=False)
    assert opt_res.selected_source == "dom"
    assert opt_res.exceeds_limit is True

    with pytest.raises(TranscriptInputTooLargeError) as exc_info:
        optimizer.optimize_by_meeting_id(meeting.id, raise_on_limit=True)
    assert exc_info.value.error_code == "INPUT_TOO_LARGE"


def test_scenario_g_deterministic_repeated_execution(
    db_session: Session, sample_user: User
) -> None:
    """Scenario G: Running full Phase 3 pipeline multiple times produces identical results."""
    dom_data = {
        "entries": [
            {"speaker": "Bob", "text": "Repeated test.", "start_time": "09:00:05"}
        ]
    }
    meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-phase3-scen-g",
        title="Deterministic Test Meeting",
        dom_capture_status="received",
        dom_transcript_data=dom_data,
    )
    db_session.add(meeting)
    db_session.commit()

    part = Participant(
        meeting_id=meeting.id,
        google_participant_name="conferenceRecords/conf-phase3-scen-g/participants/p1",
        display_name="Alice",
    )
    db_session.add(part)
    db_session.commit()

    tx = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="conferenceRecords/conf-phase3-scen-g/transcripts/t1",
        state="FILE_GENERATED",
    )
    db_session.add(tx)
    db_session.commit()

    entry = TranscriptEntry(
        transcript_id=tx.id,
        participant_id=part.id,
        google_entry_name="conferenceRecords/conf-phase3-scen-g/transcripts/t1/entries/e1",
        text="Repeated test.",
        start_time=datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    selector = TranscriptSourceSelector(db_session)
    optimizer = TranscriptTokenOptimizer(db_session)
    comp_engine = TranscriptComparisonEngine(db_session)

    # Run 1
    sel1 = selector.select_source_by_meeting_id(meeting.id)
    opt1 = optimizer.optimize_by_meeting_id(meeting.id)
    comp1 = comp_engine.compare_by_meeting_id(meeting.id)

    # Run 2
    sel2 = selector.select_source_by_meeting_id(meeting.id)
    opt2 = optimizer.optimize_by_meeting_id(meeting.id)
    comp2 = comp_engine.compare_by_meeting_id(meeting.id)

    assert sel1 == sel2
    assert opt1 == opt2
    assert comp1 == comp2


def test_scenario_h_inter_step_contracts(
    db_session: Session, sample_user: User
) -> None:
    """Inter-step contract tests: 3A -> 3C, 3B -> 3C, 3C -> 3D, 3A/3B -> 3E."""
    meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-phase3-contract",
        title="Contract Test Meeting",
    )
    db_session.add(meeting)
    db_session.commit()

    part = Participant(
        meeting_id=meeting.id,
        google_participant_name="conferenceRecords/conf-phase3-contract/participants/p1",
        display_name="Contract User",
    )
    db_session.add(part)
    db_session.commit()

    tx = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="conferenceRecords/conf-phase3-contract/transcripts/t1",
        state="FILE_GENERATED",
    )
    db_session.add(tx)
    db_session.commit()

    entry = TranscriptEntry(
        transcript_id=tx.id,
        participant_id=part.id,
        google_entry_name="conferenceRecords/conf-phase3-contract/transcripts/t1/entries/e1",
        text="Contract statement.",
        start_time=datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    # 3A -> 3C
    off_norm = OfficialTranscriptNormalizer(db_session).normalize_by_meeting_id(meeting.id)
    dom_norm = DomTranscriptNormalizer(db_session).normalize_by_meeting_id(meeting.id)

    sel_res = TranscriptSourceSelector(db_session).select_source_by_meeting_id(meeting.id)
    assert sel_res.selected_source == "official"
    assert sel_res.transcript_text == off_norm

    # 3C -> 3D
    opt_res = TranscriptTokenOptimizer(db_session).optimize_by_meeting_id(meeting.id)
    assert opt_res.selected_source == sel_res.selected_source
    assert opt_res.character_count == len(opt_res.optimized_text)

    # 3A/3B -> 3E
    comp_res = TranscriptComparisonEngine(db_session).compare_by_meeting_id(meeting.id)
    assert comp_res.meeting_id == meeting.id
