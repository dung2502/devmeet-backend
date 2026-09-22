import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.integrations.n8n import (
    N8nClient,
    N8nConnectionError,
    N8nExecutionError,
    N8nMalformedResponseError,
    N8nTimeoutError,
)
from app.main import app
from app.models import (
    Meeting,
    Participant,
    Transcript,
    TranscriptEntry,
    User,
)
from app.repositories import MeetingRepository, UserRepository
from app.services.meeting_ai import MeetingAIService


@pytest.fixture()
def sample_user(db_session: Session) -> User:
    user = User(
        google_user_id="google-user-4e-001",
        email="devmeet.step4e@example.com",
        display_name="Step4E User",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def client(db_session: Session, sample_user: User) -> TestClient:
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_get_current_user():
        return sample_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()




@pytest.fixture()
def sample_meeting_with_official(db_session: Session, sample_user: User) -> Meeting:
    meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name=f"conferenceRecords/conf-{uuid.uuid4().hex[:8]}",
        meeting_space_name="spaces/space-4e-001",
        meeting_url="https://meet.google.com/abc-defg-hij",
        title="Sprint Planning Meeting",
        start_time=datetime(2026, 9, 4, 10, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 4, 11, 0, 0, tzinfo=timezone.utc),
        ai_status="NOT_PROCESSED",
        sheets_sync_status="NOT_SYNCED",
    )
    db_session.add(meeting)
    db_session.commit()
    db_session.refresh(meeting)

    participant = Participant(
        meeting_id=meeting.id,
        google_participant_name="users/user-p1",
        display_name="Alice Engineer",
        email="alice@example.com",
    )
    db_session.add(participant)
    db_session.commit()

    transcript = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="transcripts/trans-001",
        state="ENDED",
    )
    db_session.add(transcript)
    db_session.commit()

    entry1 = TranscriptEntry(
        transcript_id=transcript.id,
        google_entry_name="entries/entry-001",
        participant_id=participant.id,
        text="Let's decide on PostgreSQL schema migration strategy for Phase 4.",
        start_time=datetime(2026, 9, 4, 10, 5, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 4, 10, 6, 0, tzinfo=timezone.utc),
    )
    entry2 = TranscriptEntry(
        transcript_id=transcript.id,
        google_entry_name="entries/entry-002",
        participant_id=participant.id,
        text="Alice will complete the n8n webhook integration by Friday.",
        start_time=datetime(2026, 9, 4, 10, 10, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 4, 10, 11, 0, tzinfo=timezone.utc),
    )
    db_session.add_all([entry1, entry2])
    db_session.commit()
    db_session.refresh(meeting)
    return meeting


@pytest.fixture()
def mock_ai_result() -> dict:
    return {
        "summary": {
            "title": "Sprint Planning Meeting Summary",
            "overview": "Discussed PostgreSQL schema migration and n8n webhook integration.",
            "key_topics": ["Database migration", "n8n Webhook"],
        },
        "decisions": [
            {
                "topic": "Migration Strategy",
                "decision": "Use PostgreSQL schema migration for Phase 4",
                "rationale": "Ensures clean isolation",
                "made_by": "Alice Engineer",
            }
        ],
        "action_items": [
            {
                "description": "Complete n8n webhook integration",
                "assignee": "Alice Engineer",
                "due_date": "2026-09-08",
                "status": "pending",
            }
        ],
        "follow_up_email": {
            "subject": "Action items from Sprint Planning",
            "body": "Hi team, please review the action items.",
            "recipients": ["alice@example.com"],
        },
    }


# ==============================================================================
# TEST A: AI_PROCESS Full Success (Phase 3 + n8n -> DB COMPLETED + SYNCED)
# ==============================================================================
def test_ai_process_success(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    n8n_response_payload = {
        "status": "success",
        "request_id": "req-mock-123",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "result": mock_ai_result,
        "observability": {
            "provider": "google",
            "model": "gemini-2.5-flash",
            "execution_time_ms": 1200,
        },
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_response_payload) as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["ai_status"] == "COMPLETED"
        assert data["sheets_sync_status"] == "SYNCED"
        assert data["ai_output"]["summary"]["title"] == "Sprint Planning Meeting Summary"
        assert data["observability"]["selected_source"] == "official"

        # Verify n8n was called with correct payload
        mock_trigger.assert_called_once()
        call_payload = mock_trigger.call_args[0][0]
        assert call_payload["action"] == "AI_PROCESS"
        assert call_payload["meeting_id"] == str(sample_meeting_with_official.id)
        assert call_payload["selected_source"] == "official"
        assert "Alice will complete the n8n webhook" in call_payload["transcript_prompt_text"]

        # Verify DB state
        meeting_repo = MeetingRepository(db_session)
        updated = meeting_repo.get_by_id(sample_meeting_with_official.id)
        assert updated.ai_status == "COMPLETED"
        assert updated.sheets_sync_status == "SYNCED"
        assert updated.ai_result is not None


# ==============================================================================
# TEST B: AI_PROCESS Partial Success (AI COMPLETED, Sheets FAILED)
# ==============================================================================
def test_ai_process_partial_success_sheets_failed(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    n8n_response_payload = {
        "status": "partial_success",
        "request_id": "req-mock-124",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "FAILED",
        "warnings": ["Google Sheets sync failed: 403 Forbidden"],
        "result": mock_ai_result,
        "observability": {
            "provider": "google",
            "model": "gemini-2.5-flash",
            "execution_time_ms": 1100,
        },
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_response_payload):
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "partial_success"
        assert data["ai_status"] == "COMPLETED"
        assert data["sheets_sync_status"] == "FAILED"
        assert data["sheets_error_warning"] is not None

        # Verify DB state: ai_result preserved, sheets_sync_status=FAILED
        meeting_repo = MeetingRepository(db_session)
        updated = meeting_repo.get_by_id(sample_meeting_with_official.id)
        assert updated.ai_status == "COMPLETED"
        assert updated.sheets_sync_status == "FAILED"
        assert updated.ai_result is not None


# ==============================================================================
# TEST C: SHEETS_RETRY (Uses cached ai_result, 0 AI calls, 0 Phase 3 calls)
# ==============================================================================
def test_sheets_retry_uses_cached_ai_result_and_no_ai_call(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    # Set meeting state to COMPLETED + FAILED
    meeting_repo = MeetingRepository(db_session)
    meeting_repo.update(
        sample_meeting_with_official.id,
        {
            "ai_status": "COMPLETED",
            "sheets_sync_status": "FAILED",
            "ai_result": mock_ai_result,
        },
    )

    n8n_response_payload = {
        "status": "success",
        "request_id": "req-mock-retry",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "result": mock_ai_result,
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_response_payload) as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["sheets_sync_status"] == "SYNCED"

        # Verify SHEETS_RETRY action was sent with cached_ai_result
        mock_trigger.assert_called_once()
        call_payload = mock_trigger.call_args[0][0]
        assert call_payload["action"] == "SHEETS_RETRY"
        assert "transcript_prompt_text" not in call_payload
        assert call_payload["cached_ai_result"] == mock_ai_result

        # Verify DB is now SYNCED
        updated = meeting_repo.get_by_id(sample_meeting_with_official.id)
        assert updated.ai_status == "COMPLETED"
        assert updated.sheets_sync_status == "SYNCED"


# ==============================================================================
# TEST D: SHEETS_RETRY Failure Preserves AI Result
# ==============================================================================
def test_sheets_retry_failure_preserves_ai_result(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    meeting_repo = MeetingRepository(db_session)
    meeting_repo.update(
        sample_meeting_with_official.id,
        {
            "ai_status": "COMPLETED",
            "sheets_sync_status": "FAILED",
            "ai_result": mock_ai_result,
        },
    )

    n8n_response_payload = {
        "status": "partial_success",
        "request_id": "req-mock-retry-fail",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "FAILED",
        "warnings": ["Quota exceeded on Sheets API"],
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_response_payload) as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "partial_success"
        assert data["sheets_sync_status"] == "FAILED"

        # Verify DB still has COMPLETED + FAILED + intact ai_result
        updated = meeting_repo.get_by_id(sample_meeting_with_official.id)
        assert updated.ai_status == "COMPLETED"
        assert updated.sheets_sync_status == "FAILED"
        assert updated.ai_result == mock_ai_result


# ==============================================================================
# TEST E: Cache Hit (Fully Synced -> 0 n8n calls, 0 AI calls)
# ==============================================================================
def test_cache_hit_fully_synced_zero_n8n_calls(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    meeting_repo = MeetingRepository(db_session)
    meeting_repo.update(
        sample_meeting_with_official.id,
        {
            "ai_status": "COMPLETED",
            "sheets_sync_status": "SYNCED",
            "ai_result": mock_ai_result,
        },
    )

    with patch.object(N8nClient, "trigger_workflow") as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["ai_status"] == "COMPLETED"
        assert data["sheets_sync_status"] == "SYNCED"
        assert data["observability"]["cache_hit"] is True
        assert data["ai_output"] == mock_ai_result

        # Zero n8n calls
        mock_trigger.assert_not_called()


# ==============================================================================
# TEST F: force_reprocess=True Bypasses Cache and Re-runs Full AI_PROCESS
# ==============================================================================
def test_force_reprocess_bypasses_cache_and_reruns_ai(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    meeting_repo = MeetingRepository(db_session)
    meeting_repo.update(
        sample_meeting_with_official.id,
        {
            "ai_status": "COMPLETED",
            "sheets_sync_status": "SYNCED",
            "ai_result": mock_ai_result,
        },
    )

    new_ai_result = dict(mock_ai_result)
    new_ai_result["summary"] = {"title": "Reprocessed Summary", "overview": "Fresh overview"}

    n8n_response_payload = {
        "status": "success",
        "request_id": "req-force",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "result": new_ai_result,
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_response_payload) as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": True},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ai_output"]["summary"]["title"] == "Reprocessed Summary"

        mock_trigger.assert_called_once()
        assert mock_trigger.call_args[0][0]["action"] == "AI_PROCESS"
        assert mock_trigger.call_args[0][0]["force_reprocess"] is True


# ==============================================================================
# TEST G: Transcript Not Ready -> 400 (0 n8n calls)
# ==============================================================================
def test_transcript_not_ready_returns_400(
    client: TestClient,
    db_session: Session,
    sample_user: User,
) -> None:
    empty_meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-empty-001",
        title="Empty Meeting",
        ai_status="NOT_PROCESSED",
        sheets_sync_status="NOT_SYNCED",
    )
    db_session.add(empty_meeting)
    db_session.commit()
    db_session.refresh(empty_meeting)

    with patch.object(N8nClient, "trigger_workflow") as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{empty_meeting.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 400
        data = res.json()
        assert data["error"]["code"] == "TRANSCRIPT_NOT_READY"
        mock_trigger.assert_not_called()

        repo = MeetingRepository(db_session)
        assert repo.get_by_id(empty_meeting.id).ai_status == "NOT_PROCESSED"


# ==============================================================================
# TEST H: Phase 3 Token Limit Exceeded -> 422 INPUT_TOO_LARGE
# ==============================================================================
def test_input_too_large_returns_422(
    client: TestClient,
    db_session: Session,
    sample_user: User,
) -> None:
    large_meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-large-001",
        title="Large Meeting",
        ai_status="NOT_PROCESSED",
        sheets_sync_status="NOT_SYNCED",
    )
    db_session.add(large_meeting)
    db_session.commit()

    transcript = Transcript(
        meeting_id=large_meeting.id,
        google_transcript_name="transcripts/trans-large-001",
        state="ENDED",
    )
    db_session.add(transcript)
    db_session.commit()

    # 130,000 characters > 120,000 max limit
    entry = TranscriptEntry(
        transcript_id=transcript.id,
        google_entry_name="entries/large-001",
        text="A" * 130_000,
        start_time=datetime(2026, 9, 4, 10, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 4, 11, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    with patch.object(N8nClient, "trigger_workflow") as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{large_meeting.id}/ai/process",
            json={},
        )
        assert res.status_code == 422
        data = res.json()
        assert data["error"]["code"] == "INPUT_TOO_LARGE"
        mock_trigger.assert_not_called()


# ==============================================================================
# TEST I: n8n Gateway Timeout -> 504 + DB ai_status remains PROCESSING
# ==============================================================================
def test_n8n_timeout_returns_504_and_preserves_processing_state(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
) -> None:
    with patch.object(
        N8nClient,
        "trigger_workflow",
        side_effect=N8nTimeoutError("Workflow execution timed out after 120s"),
    ):
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={},
        )
        assert res.status_code == 504
        data = res.json()
        assert data["error"]["code"] == "AI_GATEWAY_TIMEOUT"

        # Check DB state was kept in PROCESSING to protect against blind duplicate execution
        repo = MeetingRepository(db_session)
        updated = repo.get_by_id(sample_meeting_with_official.id)
        assert updated.ai_status == "PROCESSING"


# ==============================================================================
# TEST J: n8n Malformed / Incomplete Output -> 502 + DB ai_status=FAILED
# ==============================================================================
def test_n8n_malformed_response_returns_502_and_sets_db_failed(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
) -> None:
    malformed_payload = {
        "status": "success",
        "request_id": "req-bad",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "result": {"random_field": 123},
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=malformed_payload):
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={},
        )
        assert res.status_code == 502
        data = res.json()
        assert data["error"]["code"] == "AI_EXECUTION_FAILED"

        repo = MeetingRepository(db_session)
        updated = repo.get_by_id(sample_meeting_with_official.id)
        assert updated.ai_status == "FAILED"


# ==============================================================================
# TEST K: Concurrent Request Processing Conflict -> 409 (both force_reprocess=False and True)
# ==============================================================================
def test_concurrent_processing_conflict_returns_409_force_false(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
) -> None:
    repo = MeetingRepository(db_session)
    repo.update(sample_meeting_with_official.id, {"ai_status": "PROCESSING"})

    with patch.object(N8nClient, "trigger_workflow") as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 409
        data = res.json()
        assert data["error"]["code"] == "MEETING_AI_PROCESSING"
        mock_trigger.assert_not_called()

    # Verify DB state untouched
    assert repo.get_by_id(sample_meeting_with_official.id).ai_status == "PROCESSING"


def test_concurrent_processing_conflict_returns_409_force_true(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
) -> None:
    repo = MeetingRepository(db_session)
    repo.update(sample_meeting_with_official.id, {"ai_status": "PROCESSING"})

    with patch.object(N8nClient, "trigger_workflow") as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": True},
        )
        assert res.status_code == 409
        data = res.json()
        assert data["error"]["code"] == "MEETING_AI_PROCESSING"
        mock_trigger.assert_not_called()

    # Verify DB state untouched
    assert repo.get_by_id(sample_meeting_with_official.id).ai_status == "PROCESSING"


# ==============================================================================
# TEST L: GET /api/v1/meetings/{meeting_id}/ai/status
# ==============================================================================
def test_get_meeting_ai_status(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    meeting_id = sample_meeting_with_official.id
    repo = MeetingRepository(db_session)

    # 1. NOT_PROCESSED
    res = client.get(f"/api/v1/meetings/{meeting_id}/ai/status")
    assert res.status_code == 200
    data = res.json()
    assert data["ai_status"] == "NOT_PROCESSED"
    assert data["sheets_sync_status"] == "NOT_SYNCED"
    assert data["has_cached_result"] is False

    # 2. Update to COMPLETED + SYNCED
    repo.update(
        meeting_id,
        {
            "ai_status": "COMPLETED",
            "sheets_sync_status": "SYNCED",
            "ai_result": mock_ai_result,
        },
    )
    res = client.get(f"/api/v1/meetings/{meeting_id}/ai/status")
    assert res.status_code == 200
    data = res.json()
    assert data["ai_status"] == "COMPLETED"
    assert data["sheets_sync_status"] == "SYNCED"
    assert data["has_cached_result"] is True


# ==============================================================================
# TEST M: DOM Fallback Selection Verification
# ==============================================================================
def test_ai_process_uses_dom_fallback_when_official_is_empty(
    client: TestClient,
    db_session: Session,
    sample_user: User,
    mock_ai_result: dict,
) -> None:
    dom_meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-dom-001",
        title="DOM Fallback Meeting",
        ai_status="NOT_PROCESSED",
        sheets_sync_status="NOT_SYNCED",
        dom_transcript_data=[
            {
                "speaker": "Bob Developer",
                "text": "Captured from DOM captions.",
                "timestamp": "2026-09-04T10:00:00Z",
            }
        ],
    )
    db_session.add(dom_meeting)
    db_session.commit()
    db_session.refresh(dom_meeting)

    n8n_response_payload = {
        "status": "success",
        "request_id": "req-dom-001",
        "meeting_id": str(dom_meeting.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "result": mock_ai_result,
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_response_payload) as mock_trigger:
        res = client.post(f"/api/v1/meetings/{dom_meeting.id}/ai/process", json={})
        assert res.status_code == 200
        call_payload = mock_trigger.call_args[0][0]
        assert call_payload["selected_source"] == "dom"
        assert "Captured from DOM captions" in call_payload["transcript_prompt_text"]


# ==============================================================================
# TEST N: N8nClient Unit Tests & Webhook URL Verification
# ==============================================================================
def test_n8n_webhook_url_resolution() -> None:
    settings = get_settings()
    assert settings.n8n_webhook_url == "http://localhost:5678/webhook/devmeet-meeting-ai"
    client = N8nClient()
    assert client.webhook_url == "http://localhost:5678/webhook/devmeet-meeting-ai"


def test_n8n_client_headers_and_payload_dispatch() -> None:
    client = N8nClient(
        webhook_url="http://localhost:5678/webhook/devmeet-meeting-ai",
        webhook_secret="test-secret-123",
        timeout=10.0,
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"status": "success", "ai_status": "COMPLETED"}

    mock_http = MagicMock()
    mock_http.post.return_value = mock_response
    client.http_client = mock_http

    payload = {"action": "AI_PROCESS", "meeting_id": "123"}
    resp = client.trigger_workflow(payload)

    assert resp["status"] == "success"
    mock_http.post.assert_called_once_with(
        "http://localhost:5678/webhook/devmeet-meeting-ai",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Secret": "test-secret-123",
        },
        timeout=10.0,
    )


def test_n8n_client_timeout_handling() -> None:
    client = N8nClient(
        webhook_url="http://localhost:5678/webhook/devmeet-meeting-ai",
        webhook_secret="test-secret-123",
    )
    mock_http = MagicMock()
    mock_http.post.side_effect = httpx.TimeoutException("Timeout")
    client.http_client = mock_http

    with pytest.raises(N8nTimeoutError):
        client.trigger_workflow({"action": "AI_PROCESS"})


def test_n8n_client_network_error_handling() -> None:
    client = N8nClient(
        webhook_url="http://localhost:5678/webhook/devmeet-meeting-ai",
        webhook_secret="test-secret-123",
    )
    mock_http = MagicMock()
    mock_http.post.side_effect = httpx.ConnectError("Connection refused")
    client.http_client = mock_http

    with pytest.raises(N8nConnectionError):
        client.trigger_workflow({"action": "AI_PROCESS"})


# ==============================================================================
# TEST O: External Response Loss Simulation & Retry Protection (Section 13 Regression)
# ==============================================================================
def test_external_response_loss_simulation_and_retry_protection(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
) -> None:
    repo = MeetingRepository(db_session)
    assert repo.get_by_id(sample_meeting_with_official.id).ai_status == "NOT_PROCESSED"

    # Step 1: Initial call times out (simulates external execution completed in background but HTTP lost)
    with patch.object(
        N8nClient,
        "trigger_workflow",
        side_effect=N8nTimeoutError("External execution timed out on HTTP response"),
    ) as mock_trigger:
        res1 = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res1.status_code == 504
        assert res1.json()["error"]["code"] == "AI_GATEWAY_TIMEOUT"
        mock_trigger.assert_called_once()

    # Verify DB state is durably PROCESSING (not reset to NOT_PROCESSED or FAILED)
    meeting_after_timeout = repo.get_by_id(sample_meeting_with_official.id)
    assert meeting_after_timeout.ai_status == "PROCESSING"

    # Step 2: Client retries same logical request with force_reprocess=False -> 409
    with patch.object(N8nClient, "trigger_workflow") as mock_trigger2:
        res2 = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res2.status_code == 409
        assert res2.json()["error"]["code"] == "MEETING_AI_PROCESSING"
        mock_trigger2.assert_not_called()

    # Step 3: Client retries with force_reprocess=True while state is still PROCESSING -> MUST ALSO RETURN 409
    with patch.object(N8nClient, "trigger_workflow") as mock_trigger3:
        res3 = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": True},
        )
        assert res3.status_code == 409
        assert res3.json()["error"]["code"] == "MEETING_AI_PROCESSING"
        # 0 n8n calls made!
        mock_trigger3.assert_not_called()

    # Verify DB state remains unchanged in PROCESSING
    final_meeting = repo.get_by_id(sample_meeting_with_official.id)
    assert final_meeting.ai_status == "PROCESSING"


# ==============================================================================
# TEST P: force_reprocess=True on resolved states (FAILED, NOT_PROCESSED)
# ==============================================================================
def test_force_reprocess_on_failed_state_triggers_ai_process(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    repo = MeetingRepository(db_session)
    repo.update(sample_meeting_with_official.id, {"ai_status": "FAILED"})

    n8n_success_payload = {
        "status": "success",
        "request_id": "req-failed-force",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "result": mock_ai_result,
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_success_payload) as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": True},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "success"
        mock_trigger.assert_called_once()
        assert mock_trigger.call_args[0][0]["action"] == "AI_PROCESS"
        assert mock_trigger.call_args[0][0]["force_reprocess"] is True

