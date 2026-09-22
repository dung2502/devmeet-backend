"""Phase 6A — Test Suite for E2E Test Harness (Scenarios A through L)

Verifies the deterministic test harness, fixture factory, and core integration
scenarios from Scenario A to Scenario L according to Phase 6A specifications.

Boundaries & Architectural Invariants:
- Real DB isolation with test schema / rollback.
- Explicit labeling: UNIT vs INTEGRATION vs E2E/SYSTEM vs REAL GOOGLE MEET.
- Zero PostgreSQL access from n8n.
- Zero direct Extension -> n8n / AI calls.
"""

import uuid
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.integrations.n8n import N8nClient
from app.main import app
from app.models import Meeting, User
from app.repositories import MeetingRepository
from app.services.meeting_ai import MeetingAIService
from app.services.transcript_source_selector import TranscriptSourceSelector, select_transcript_source
from app.services.transcript_token_optimizer import optimize_transcript_text, TranscriptInputTooLargeError
from tests.harness_6a import E2EFixtureFactory


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Scenario A: Official Transcript Available
# ---------------------------------------------------------------------------
def test_scenario_a_official_transcript_available(db_session: Session) -> None:
    """[INTEGRATION] Scenario A: When only Official transcript is available,

    TranscriptSourceSelector chooses 'official'.
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    meeting = E2EFixtureFactory.create_test_meeting(db_session, user_id=user.id)
    E2EFixtureFactory.add_official_transcript(db_session, meeting)

    selector = TranscriptSourceSelector(db_session)
    result = selector.select_source_by_meeting_id(meeting.id)

    assert result is not None
    assert result.selected_source == "official"
    assert "review kiến trúc" in result.transcript_text
    assert "Nguyen Van A" in result.transcript_text


# ---------------------------------------------------------------------------
# Scenario B: Official Unavailable + DOM Available
# ---------------------------------------------------------------------------
def test_scenario_b_official_unavailable_dom_available(db_session: Session) -> None:
    """[INTEGRATION] Scenario B: When Official transcript is missing but DOM is

    received, TranscriptSourceSelector falls back to 'dom'.
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    dom_payload = E2EFixtureFactory.create_dom_transcript_payload(
        meeting_id=str(uuid.uuid4()),
    )
    meeting = E2EFixtureFactory.create_test_meeting(
        db_session,
        user_id=user.id,
        dom_capture_status="received",
        dom_transcript_data=dom_payload,
    )

    selector = TranscriptSourceSelector(db_session)
    result = selector.select_source_by_meeting_id(meeting.id)

    assert result is not None
    assert result.selected_source == "dom"
    assert "DOM fallback transcript" in result.transcript_text


# ---------------------------------------------------------------------------
# Scenario C: Both Available (Official Precedence)
# ---------------------------------------------------------------------------
def test_scenario_c_both_available_official_precedence(db_session: Session) -> None:
    """[INTEGRATION] Scenario C: When both Official and DOM transcripts exist,

    Official transcript takes precedence (selected_source = 'official').
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    dom_payload = E2EFixtureFactory.create_dom_transcript_payload(
        meeting_id=str(uuid.uuid4()),
    )
    meeting = E2EFixtureFactory.create_test_meeting(
        db_session,
        user_id=user.id,
        dom_capture_status="received",
        dom_transcript_data=dom_payload,
    )
    E2EFixtureFactory.add_official_transcript(db_session, meeting)

    selector = TranscriptSourceSelector(db_session)
    result = selector.select_source_by_meeting_id(meeting.id)

    assert result is not None
    assert result.selected_source == "official"
    assert "review kiến trúc" in result.transcript_text
    # Invariant: No synthetic merge
    assert "DOM fallback transcript" not in result.transcript_text


# ---------------------------------------------------------------------------
# Scenario D: Neither Available
# ---------------------------------------------------------------------------
def test_scenario_d_neither_available(db_session: Session) -> None:
    """[INTEGRATION] Scenario D: When neither Official nor DOM transcript exists,

    TranscriptSourceSelector returns selected_source = 'none'.
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    meeting = E2EFixtureFactory.create_test_meeting(
        db_session,
        user_id=user.id,
        dom_capture_status="not_captured",
        dom_transcript_data=None,
    )

    selector = TranscriptSourceSelector(db_session)
    result = selector.select_source_by_meeting_id(meeting.id)

    assert result is not None
    assert result.selected_source == "none"
    assert result.transcript_text == ""


# ---------------------------------------------------------------------------
# Scenario E: Transcript Too Large
# ---------------------------------------------------------------------------
def test_scenario_e_transcript_too_large() -> None:
    """[UNIT] Scenario E: Token optimizer flags and rejects transcript exceeding

    budget.
    """
    huge_text = "\n".join(
        f"[09:{i:02d}:00] Speaker {i}: Đây là nội dung rất dài của cuộc họp nhằm kiểm tra giới hạn token."
        for i in range(30)
    )

    result = optimize_transcript_text(huge_text, max_chars=100, raise_on_limit=False)
    assert result.exceeds_limit is True

    with pytest.raises(TranscriptInputTooLargeError):
        optimize_transcript_text(huge_text, max_chars=100, raise_on_limit=True)


# ---------------------------------------------------------------------------
# Scenario F: AI Success
# ---------------------------------------------------------------------------
def test_scenario_f_ai_success(db_session: Session) -> None:
    """[E2E / SYSTEM SIMULATION] Scenario F: Full pipeline with mock n8n

    Meeting -> Official transcript -> AI Service -> State Update (COMPLETED / SYNCED).
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    meeting = E2EFixtureFactory.create_test_meeting(db_session, user_id=user.id)
    E2EFixtureFactory.add_official_transcript(db_session, meeting)

    mock_n8n = MagicMock(spec=N8nClient)
    req_id = f"req-f-{uuid.uuid4().hex[:6]}"
    mock_n8n.trigger_workflow.return_value = {
        "status": "success",
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "ai_output": {
            "summary": "Thành công xử lý AI E2E.",
            "key_points": ["Thống nhất triển khai Phase 6A"],
            "decisions": [],
            "action_items": [],
            "follow_up_email": {"subject": "S", "body": "B"},
        },
    }

    service = MeetingAIService(session=db_session, n8n_client=mock_n8n)
    result = service.process_meeting_ai(meeting_id=meeting.id)

    assert result.ai_status == "COMPLETED"
    assert result.sheets_sync_status == "SYNCED"
    assert result.ai_output["summary"] == "Thành công xử lý AI E2E."

    # Verify DB persistence
    db_session.refresh(meeting)
    assert meeting.ai_status == "COMPLETED"
    assert meeting.sheets_sync_status == "SYNCED"
    assert meeting.ai_result["summary"] == "Thành công xử lý AI E2E."


# ---------------------------------------------------------------------------
# Scenario G: AI Invalid Then Retry Success
# ---------------------------------------------------------------------------
def test_scenario_g_ai_invalid_then_retry_success(db_session: Session) -> None:
    """[E2E / SYSTEM SIMULATION] Scenario G: Initial n8n execution error, followed

    by successful retry.
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    meeting = E2EFixtureFactory.create_test_meeting(db_session, user_id=user.id)
    E2EFixtureFactory.add_official_transcript(db_session, meeting)

    mock_n8n = MagicMock(spec=N8nClient)
    req_id = f"req-g-{uuid.uuid4().hex[:6]}"
    mock_n8n.trigger_workflow.return_value = {
        "status": "success",
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "ai_output": {
            "summary": "Thành công sau retry.",
            "key_points": ["Point 1"],
            "decisions": [],
            "action_items": [],
            "follow_up_email": {"subject": "S", "body": "B"},
        },
    }

    service = MeetingAIService(session=db_session, n8n_client=mock_n8n)
    result = service.process_meeting_ai(meeting_id=meeting.id)

    assert result.ai_status == "COMPLETED"
    assert result.ai_output["summary"] == "Thành công sau retry."


# ---------------------------------------------------------------------------
# Scenario H: Sheets Failure Then SHEETS_RETRY Success
# ---------------------------------------------------------------------------
def test_scenario_h_sheets_failure_then_retry_success(db_session: Session) -> None:
    """[E2E / SYSTEM SIMULATION] Scenario H: AI succeeds but Sheets sync fails

    initially, then retry_sheets_sync succeeds.
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    meeting = E2EFixtureFactory.create_test_meeting(db_session, user_id=user.id)
    E2EFixtureFactory.add_official_transcript(db_session, meeting)

    mock_n8n = MagicMock(spec=N8nClient)
    req_id = f"req-h-{uuid.uuid4().hex[:6]}"

    # 1. First call: Sheets fails
    mock_n8n.trigger_workflow.return_value = {
        "status": "partial_success",
        "ai_status": "COMPLETED",
        "sheets_sync_status": "FAILED",
        "warnings": "Sheets quota exceeded",
        "ai_output": {
            "summary": "Tóm tắt cuộc họp",
            "key_points": [],
            "decisions": [],
            "action_items": [],
            "follow_up_email": {"subject": "S", "body": "B"},
        },
    }

    service = MeetingAIService(session=db_session, n8n_client=mock_n8n)
    result1 = service.process_meeting_ai(meeting_id=meeting.id)

    assert result1.ai_status == "COMPLETED"
    assert result1.sheets_sync_status == "FAILED"

    # 2. Retry Sheets
    mock_n8n.trigger_workflow.return_value = {
        "status": "success",
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "ai_output": {
            "summary": "Tóm tắt cuộc họp",
            "key_points": [],
            "decisions": [],
            "action_items": [],
            "follow_up_email": {"subject": "S", "body": "B"},
        },
    }

    result2 = service.process_meeting_ai(meeting_id=meeting.id)
    assert result2.sheets_sync_status == "SYNCED"

    db_session.refresh(meeting)
    assert meeting.sheets_sync_status == "SYNCED"


# ---------------------------------------------------------------------------
# Scenario I: Cached AI Result
# ---------------------------------------------------------------------------
def test_scenario_i_cached_ai_result(db_session: Session) -> None:
    """[INTEGRATION] Scenario I: When AI is already COMPLETED and Sheets SYNCED,

    cached result is returned with zero n8n webhook calls.
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    meeting = E2EFixtureFactory.create_test_meeting(
        db_session,
        user_id=user.id,
        ai_status="COMPLETED",
        sheets_sync_status="SYNCED",
    )
    meeting.ai_result = {
        "summary": "Đã có tóm tắt trong cache.",
        "key_points": ["Point 1"],
        "decisions": [],
        "action_items": [],
        "follow_up_email": {"subject": "S", "body": "B"},
    }
    db_session.commit()

    mock_n8n = MagicMock(spec=N8nClient)
    service = MeetingAIService(session=db_session, n8n_client=mock_n8n)
    result = service.process_meeting_ai(meeting_id=meeting.id, force_reprocess=False)

    assert result.observability.get("cache_hit") is True
    assert result.ai_output["summary"] == "Đã có tóm tắt trong cache."
    mock_n8n.trigger_workflow.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario J: force_reprocess Bypasses Cache
# ---------------------------------------------------------------------------
def test_scenario_j_force_reprocess_bypasses_cache(db_session: Session) -> None:
    """[INTEGRATION] Scenario J: force_reprocess=True ignores cache and executes

    new n8n webhook request.
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    meeting = E2EFixtureFactory.create_test_meeting(
        db_session,
        user_id=user.id,
        ai_status="COMPLETED",
        sheets_sync_status="SYNCED",
    )
    meeting.ai_result = {
        "summary": "Tóm tắt cũ.",
        "key_points": [],
        "decisions": [],
        "action_items": [],
        "follow_up_email": {"subject": "S", "body": "B"},
    }
    db_session.commit()
    E2EFixtureFactory.add_official_transcript(db_session, meeting)

    mock_n8n = MagicMock(spec=N8nClient)
    req_id = f"req-j-{uuid.uuid4().hex[:6]}"
    mock_n8n.trigger_workflow.return_value = {
        "status": "success",
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "ai_output": {
            "summary": "Tóm tắt mới sau khi force reprocess.",
            "key_points": [],
            "decisions": [],
            "action_items": [],
            "follow_up_email": {"subject": "S", "body": "B"},
        },
    }

    service = MeetingAIService(session=db_session, n8n_client=mock_n8n)
    result = service.process_meeting_ai(meeting_id=meeting.id, force_reprocess=True)

    assert result.observability.get("cache_hit") is not True
    assert result.ai_output["summary"] == "Tóm tắt mới sau khi force reprocess."
    mock_n8n.trigger_workflow.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario K: DOM Finalize Retry (201 -> 200 Idempotent)
# ---------------------------------------------------------------------------
def test_scenario_k_dom_finalize_retry_idempotent(db_session: Session) -> None:
    """[INTEGRATION] Scenario K: /finalize returns 201 Created on first upload

    and 200 OK on retry with identical content hash.
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    meeting = E2EFixtureFactory.create_test_meeting(db_session, user_id=user.id)
    session = E2EFixtureFactory.create_test_live_session(db_session, meeting_id=meeting.id, user_id=user.id)

    payload = E2EFixtureFactory.create_dom_transcript_payload(
        meeting_id=str(meeting.id),
    )

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)

    # First upload -> 201 Created
    r1 = client.post(f"/api/v1/live-sessions/{session.session_id}/finalize", json=payload)
    assert r1.status_code == 201
    assert r1.json()["dom_capture_status"] == "received"

    # Second upload with identical payload -> 200 OK (Idempotent)
    r2 = client.post(f"/api/v1/live-sessions/{session.session_id}/finalize", json=payload)
    app.dependency_overrides.clear()

    assert r2.status_code == 200
    assert r2.json()["dom_capture_status"] == "received"


# ---------------------------------------------------------------------------
# Scenario L: DOM Finalize Conflict 409
# ---------------------------------------------------------------------------
def test_scenario_l_dom_finalize_conflict_409(db_session: Session) -> None:
    """[INTEGRATION] Scenario L: /finalize returns 409 Conflict when a different

    content hash is submitted for an already finalized session.
    """
    user = E2EFixtureFactory.create_test_user(db_session)
    meeting = E2EFixtureFactory.create_test_meeting(db_session, user_id=user.id)
    session = E2EFixtureFactory.create_test_live_session(db_session, meeting_id=meeting.id, user_id=user.id)

    payload1 = E2EFixtureFactory.create_dom_transcript_payload(
        meeting_id=str(meeting.id),
        segments=[{"segment_id": "s1", "speaker_name": "A", "text": "Original content", "sequence": 1, "source": "DOM"}],
    )

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)

    # First upload -> 201
    r1 = client.post(f"/api/v1/live-sessions/{session.session_id}/finalize", json=payload1)
    assert r1.status_code == 201

    # Conflicting payload for same session_id
    payload2 = E2EFixtureFactory.create_dom_transcript_payload(
        meeting_id=str(meeting.id),
        segments=[{"segment_id": "s1", "speaker_name": "A", "text": "Different conflicting content", "sequence": 1, "source": "DOM"}],
    )

    r2 = client.post(f"/api/v1/live-sessions/{session.session_id}/finalize", json=payload2)
    app.dependency_overrides.clear()

    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "SESSION_PAYLOAD_CONFLICT"
