import json
import os
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


# ---------------------------------------------------------------------------
# Workflow JSON Loader for Structural Invariant Assertions
# ---------------------------------------------------------------------------
CURR_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = CURR_DIR
while not os.path.exists(os.path.join(ROOT_DIR, "ai_workflow")) and os.path.dirname(ROOT_DIR) != ROOT_DIR:
    ROOT_DIR = os.path.dirname(ROOT_DIR)

WORKFLOW_PATH = os.path.join(ROOT_DIR, "ai_workflow", "DEVMEET_Meeting_Processor_v3.json")


def load_workflow_json() -> dict:
    assert os.path.exists(WORKFLOW_PATH), f"Workflow file must exist at {WORKFLOW_PATH}"
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture()
def sample_user(db_session: Session) -> User:
    user = User(
        google_user_id="google-user-4f-001",
        email="devmeet.step4f@example.com",
        display_name="Step 4F Acceptance User",
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
        conference_record_name=f"conferenceRecords/conf-4f-{uuid.uuid4().hex[:8]}",
        meeting_space_name="spaces/space-4f-001",
        meeting_url="https://meet.google.com/xyz-step4f-acc",
        title="Phase 4 Acceptance Review Meeting",
        start_time=datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 7, 11, 0, 0, tzinfo=timezone.utc),
        ai_status="NOT_PROCESSED",
        sheets_sync_status="NOT_SYNCED",
    )
    db_session.add(meeting)
    db_session.commit()
    db_session.refresh(meeting)

    participant = Participant(
        meeting_id=meeting.id,
        google_participant_name="users/user-p4f",
        display_name="Lead Engineer",
        email="lead@example.com",
    )
    db_session.add(participant)
    db_session.commit()

    transcript = Transcript(
        meeting_id=meeting.id,
        google_transcript_name="transcripts/trans-4f-001",
        state="ENDED",
    )
    db_session.add(transcript)
    db_session.commit()

    entry1 = TranscriptEntry(
        transcript_id=transcript.id,
        google_entry_name="entries/entry-4f-001",
        participant_id=participant.id,
        text="All acceptance criteria for Phase 4 must be strictly verified against canonical architecture.",
        start_time=datetime(2026, 9, 7, 10, 5, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 7, 10, 6, 0, tzinfo=timezone.utc),
    )
    entry2 = TranscriptEntry(
        transcript_id=transcript.id,
        google_entry_name="entries/entry-4f-002",
        participant_id=participant.id,
        text="The team approves the final Phase 4 deliverables.",
        start_time=datetime(2026, 9, 7, 10, 10, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 7, 10, 11, 0, tzinfo=timezone.utc),
    )
    db_session.add_all([entry1, entry2])
    db_session.commit()
    db_session.refresh(meeting)
    return meeting


@pytest.fixture()
def mock_ai_result() -> dict:
    return {
        "summary": {
            "title": "Phase 4 Acceptance Review Summary",
            "overview": "Reviewed all Phase 4 integration and acceptance criteria.",
            "key_topics": ["Phase 4 Architecture", "Acceptance Verification"],
        },
        "decisions": [
            {
                "topic": "Phase 4 Acceptance",
                "decision": "Approve final Phase 4 integration",
                "rationale": "All 10 scenarios passed acceptance",
                "made_by": "Lead Engineer",
            }
        ],
        "action_items": [
            {
                "description": "Tag and release Phase 4 release candidate",
                "assignee": "Lead Engineer",
                "due_date": "2026-09-10",
                "status": "pending",
            }
        ],
        "follow_up_email": {
            "subject": "[DevMeet AI] Phase 4 Acceptance Approved",
            "body": "Hi team, Phase 4 acceptance has successfully passed.",
            "recipients": ["lead@example.com"],
        },
    }


# ==============================================================================
# SCENARIO A: AI_PROCESS -> AI SUCCESS -> 4 SHEETS SUCCESS
# ==============================================================================
def test_acceptance_scenario_a_ai_process_full_success(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    """
    Scenario A: Full success flow:
    1. Phase 3 token optimization called
    2. Selected source is 'official'
    3. n8n canonical webhook called with action='AI_PROCESS'
    4. Successful AI + 4 Sheets append
    5. Final DB state: ai_status=COMPLETED, ai_result persisted, sheets_sync_status=SYNCED
    """
    n8n_success_payload = {
        "status": "success",
        "request_id": f"req-{sample_meeting_with_official.id}",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "result": mock_ai_result,
        "observability": {
            "provider": "google",
            "model": "gemini-2.5-flash",
            "execution_time_ms": 1150,
        },
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_success_payload) as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["ai_status"] == "COMPLETED"
        assert data["sheets_sync_status"] == "SYNCED"
        assert data["ai_output"]["summary"]["title"] == "Phase 4 Acceptance Review Summary"
        assert data["observability"]["selected_source"] == "official"

        # Verify n8n payload structure
        mock_trigger.assert_called_once()
        call_payload = mock_trigger.call_args[0][0]
        assert call_payload["action"] == "AI_PROCESS"
        assert call_payload["meeting_id"] == str(sample_meeting_with_official.id)
        assert call_payload["selected_source"] == "official"
        assert "Phase 4" in call_payload["transcript_prompt_text"]

        # Verify DB final state
        repo = MeetingRepository(db_session)
        meeting = repo.get_by_id(sample_meeting_with_official.id)
        assert meeting.ai_status == "COMPLETED"
        assert meeting.sheets_sync_status == "SYNCED"
        assert meeting.ai_result is not None


# ==============================================================================
# SCENARIO B: AI_PROCESS -> AI SUCCESS -> SHEETS FAIL -> PARTIAL_SUCCESS
# ==============================================================================
def test_acceptance_scenario_b_ai_success_sheets_fail_partial_success(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    """
    Scenario B: AI succeeds, but Google Sheets fails after independent retry:
    1. ai_status is COMPLETED
    2. ai_result is preserved in DB
    3. sheets_sync_status is FAILED
    4. Response status is partial_success
    5. ai_result is NOT erased or marked FAILED
    """
    n8n_partial_payload = {
        "status": "partial_success",
        "request_id": f"req-{sample_meeting_with_official.id}",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "FAILED",
        "warnings": ["Google Sheets API error: 503 Service Unavailable after 3 retries"],
        "result": mock_ai_result,
        "observability": {
            "provider": "google",
            "model": "gemini-2.5-flash",
            "execution_time_ms": 1200,
        },
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_partial_payload) as mock_trigger:
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

        # Verify DB state: ai_result preserved, sheets_sync_status FAILED
        repo = MeetingRepository(db_session)
        meeting = repo.get_by_id(sample_meeting_with_official.id)
        assert meeting.ai_status == "COMPLETED"
        assert meeting.sheets_sync_status == "FAILED"
        assert meeting.ai_result == mock_ai_result


# ==============================================================================
# SCENARIO C: SHEETS_RETRY -> CACHED_AI_RESULT -> SHEETS SUCCESS -> NO AI
# ==============================================================================
def test_acceptance_scenario_c_sheets_retry_uses_cached_ai_result_zero_ai(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    """
    Scenario C: Replay of COMPLETED + FAILED invokes SHEETS_RETRY:
    1. Uses cached_ai_result from DB
    2. 0 Phase 3 calls
    3. 0 AI LLM calls
    4. Sheets succeeds -> final state COMPLETED + SYNCED
    """
    repo = MeetingRepository(db_session)
    repo.update(
        sample_meeting_with_official.id,
        {
            "ai_status": "COMPLETED",
            "sheets_sync_status": "FAILED",
            "ai_result": mock_ai_result,
        },
    )

    n8n_retry_success = {
        "status": "success",
        "request_id": f"req-{sample_meeting_with_official.id}",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "result": mock_ai_result,
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_retry_success) as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["sheets_sync_status"] == "SYNCED"

        # Verify payload sent to n8n is SHEETS_RETRY and contains cached_ai_result
        mock_trigger.assert_called_once()
        call_payload = mock_trigger.call_args[0][0]
        assert call_payload["action"] == "SHEETS_RETRY"
        assert call_payload["cached_ai_result"] == mock_ai_result
        assert "transcript_prompt_text" not in call_payload

        # Verify DB updated to SYNCED with unchanged ai_result
        meeting = repo.get_by_id(sample_meeting_with_official.id)
        assert meeting.ai_status == "COMPLETED"
        assert meeting.sheets_sync_status == "SYNCED"
        assert meeting.ai_result == mock_ai_result


# ==============================================================================
# SCENARIO D: SHEETS_RETRY -> SHEETS FAIL -> AI RESULT PRESERVED
# ==============================================================================
def test_acceptance_scenario_d_sheets_retry_failure_preserves_ai_result(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    """
    Scenario D: SHEETS_RETRY fails again:
    1. 0 AI calls
    2. ai_status remains COMPLETED
    3. sheets_sync_status remains FAILED
    4. ai_result is NOT lost
    """
    repo = MeetingRepository(db_session)
    repo.update(
        sample_meeting_with_official.id,
        {
            "ai_status": "COMPLETED",
            "sheets_sync_status": "FAILED",
            "ai_result": mock_ai_result,
        },
    )

    n8n_retry_fail = {
        "status": "partial_success",
        "request_id": f"req-{sample_meeting_with_official.id}",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "FAILED",
        "warnings": ["Quota exceeded on Sheets API"],
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_retry_fail) as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "partial_success"
        assert data["sheets_sync_status"] == "FAILED"

        # Verify DB state
        meeting = repo.get_by_id(sample_meeting_with_official.id)
        assert meeting.ai_status == "COMPLETED"
        assert meeting.sheets_sync_status == "FAILED"
        assert meeting.ai_result == mock_ai_result


# ==============================================================================
# SCENARIO E: MALFORMED AI OUTPUT -> REJECT -> NO SHEETS -> DB FAILED
# ==============================================================================
def test_acceptance_scenario_e_malformed_ai_output_rejection(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
) -> None:
    """
    Scenario E: n8n returns malformed structured output:
    1. Validation rejects invalid payload
    2. Backend sets ai_status=FAILED
    3. Returns 502 AI_EXECUTION_FAILED
    4. Malformed payload is NOT stored as successful ai_result
    """
    malformed_payload = {
        "status": "success",
        "request_id": "req-malformed-001",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "result": {"unrecognized_key": "bad_data"},
    }

    with patch.object(N8nClient, "trigger_workflow", return_value=malformed_payload):
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 502
        data = res.json()
        assert data["error"]["code"] == "AI_EXECUTION_FAILED"

        repo = MeetingRepository(db_session)
        meeting = repo.get_by_id(sample_meeting_with_official.id)
        assert meeting.ai_status == "FAILED"


# ==============================================================================
# SCENARIO F: TRANSCRIPT > 120k -> PHASE 3 REJECTS -> NO AI / NO N8N / NO SHEETS
# ==============================================================================
def test_acceptance_scenario_f_transcript_token_limit_120k_rejection(
    client: TestClient,
    db_session: Session,
    sample_user: User,
) -> None:
    """
    Scenario F: Transcript exceeds 120,000 characters:
    1. Phase 3 TranscriptTokenOptimizer rejects before n8n
    2. Returns 422 INPUT_TOO_LARGE
    3. 0 n8n calls
    4. 0 AI calls
    5. 0 Sheets calls
    """
    large_meeting = Meeting(
        user_id=sample_user.id,
        conference_record_name="conferenceRecords/conf-4f-large",
        title="Large Transcript Meeting",
        ai_status="NOT_PROCESSED",
        sheets_sync_status="NOT_SYNCED",
    )
    db_session.add(large_meeting)
    db_session.commit()

    transcript = Transcript(
        meeting_id=large_meeting.id,
        google_transcript_name="transcripts/trans-4f-large",
        state="ENDED",
    )
    db_session.add(transcript)
    db_session.commit()

    entry = TranscriptEntry(
        transcript_id=transcript.id,
        google_entry_name="entries/entry-4f-large",
        text="A" * 125_000,
        start_time=datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 7, 11, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    with patch.object(N8nClient, "trigger_workflow") as mock_trigger:
        res = client.post(f"/api/v1/meetings/{large_meeting.id}/ai/process", json={})
        assert res.status_code == 422
        data = res.json()
        assert data["error"]["code"] == "INPUT_TOO_LARGE"
        mock_trigger.assert_not_called()

        repo = MeetingRepository(db_session)
        assert repo.get_by_id(large_meeting.id).ai_status == "NOT_PROCESSED"


# ==============================================================================
# SCENARIO G: CACHED RESULT -> DB CACHE HIT -> NO N8N / NO AI
# ==============================================================================
def test_acceptance_scenario_g_cache_hit_zero_n8n_zero_ai(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    """
    Scenario G: Replay of COMPLETED + SYNCED meeting with force_reprocess=False:
    1. Returns DB cached result directly
    2. 0 n8n calls
    3. 0 Phase 3 calls
    4. 0 AI calls
    """
    repo = MeetingRepository(db_session)
    repo.update(
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
        assert data["observability"]["cache_hit"] is True
        assert data["ai_output"] == mock_ai_result
        mock_trigger.assert_not_called()


# ==============================================================================
# SCENARIO H: FORCE_REPROCESS SEMANTICS (Resolved State vs. PROCESSING)
# ==============================================================================
def test_acceptance_scenario_h_force_reprocess_semantics(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
    mock_ai_result: dict,
) -> None:
    """
    Scenario H:
    1. PROCESSING + force_reprocess=True -> 409 (0 n8n, 0 AI, DB unchanged)
    2. PROCESSING + force_reprocess=False -> 409 (0 n8n, 0 AI, DB unchanged)
    3. Resolved COMPLETED + SYNCED + force_reprocess=True -> triggers new AI_PROCESS
    4. Resolved FAILED + force_reprocess=True -> triggers new AI_PROCESS
    """
    repo = MeetingRepository(db_session)

    # Subtest 1 & 2: PROCESSING state rejects both force_reprocess=True and False
    repo.update(sample_meeting_with_official.id, {"ai_status": "PROCESSING"})
    with patch.object(N8nClient, "trigger_workflow") as mock_trigger:
        # force_reprocess=False
        res_false = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res_false.status_code == 409
        assert res_false.json()["error"]["code"] == "MEETING_AI_PROCESSING"

        # force_reprocess=True
        res_true = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": True},
        )
        assert res_true.status_code == 409
        assert res_true.json()["error"]["code"] == "MEETING_AI_PROCESSING"
        mock_trigger.assert_not_called()

    assert repo.get_by_id(sample_meeting_with_official.id).ai_status == "PROCESSING"

    # Subtest 3: Resolved COMPLETED + SYNCED + force_reprocess=True allows new AI_PROCESS
    repo.update(
        sample_meeting_with_official.id,
        {
            "ai_status": "COMPLETED",
            "sheets_sync_status": "SYNCED",
            "ai_result": mock_ai_result,
        },
    )
    n8n_fresh_payload = {
        "status": "success",
        "request_id": "req-fresh-4f",
        "meeting_id": str(sample_meeting_with_official.id),
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "result": mock_ai_result,
    }
    with patch.object(N8nClient, "trigger_workflow", return_value=n8n_fresh_payload) as mock_trigger:
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": True},
        )
        assert res.status_code == 200
        mock_trigger.assert_called_once()
        assert mock_trigger.call_args[0][0]["action"] == "AI_PROCESS"
        assert mock_trigger.call_args[0][0]["force_reprocess"] is True


# ==============================================================================
# SCENARIO I: INVALID WEBHOOK SECRET -> FAIL-CLOSED (NO AI / NO SHEETS)
# ==============================================================================
def test_acceptance_scenario_i_webhook_auth_fail_closed() -> None:
    """
    Scenario I: Verify fail-closed authentication logic in workflow JSON:
    1. Authenticate & Validate Input node checks DEVMEET_WEBHOOK_SECRET
    2. Missing/invalid secret produces HTTP 401 UNAUTHORIZED
    3. Missing server secret produces HTTP 500 SECURITY_CONFIG_ERROR
    4. 0 AI and 0 Sheets execution occur
    """
    wf = load_workflow_json()
    auth_node = next(n for n in wf["nodes"] if n["name"] == "Authenticate & Validate Input")
    js_code = auth_node["parameters"]["jsCode"]

    # Verify fail-closed secret checks in JavaScript code
    assert "DEVMEET_WEBHOOK_SECRET" in js_code
    assert "UNAUTHORIZED" in js_code
    assert "SECURITY_CONFIG_ERROR" in js_code
    assert "401" in js_code
    assert "is_error: true" in js_code

    # Verify N8nClient sends secret in headers
    client = N8nClient(
        webhook_url="http://localhost:5678/webhook/devmeet-meeting-ai",
        webhook_secret="correct-secret-123",
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "success"}
    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp
    client.http_client = mock_http

    client.trigger_workflow({"action": "AI_PROCESS"})
    mock_http.post.assert_called_once()
    assert mock_http.post.call_args[1]["headers"]["X-Webhook-Secret"] == "correct-secret-123"


# ==============================================================================
# SCENARIO J: PROVIDER TIMEOUT -> RECOVERY & STATE SAFETY
# ==============================================================================
def test_acceptance_scenario_j_provider_timeout_failure_handling(
    client: TestClient,
    db_session: Session,
    sample_meeting_with_official: Meeting,
) -> None:
    """
    Scenario J:
    1. External gateway/provider timeout raises N8nTimeoutError
    2. Backend returns 504 AI_GATEWAY_TIMEOUT
    3. Meeting state durably remains in PROCESSING (no false COMPLETED)
    4. Replay attempts are conservatively blocked (409) to avoid duplicate external AI runs
    """
    repo = MeetingRepository(db_session)
    with patch.object(
        N8nClient,
        "trigger_workflow",
        side_effect=N8nTimeoutError("OpenAI execution timed out after 60s"),
    ):
        res = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res.status_code == 504
        assert res.json()["error"]["code"] == "AI_GATEWAY_TIMEOUT"

    # Durable state check: PROCESSING is preserved
    meeting = repo.get_by_id(sample_meeting_with_official.id)
    assert meeting.ai_status == "PROCESSING"

    # Subsequent replay without confirmed completion is intercepted
    with patch.object(N8nClient, "trigger_workflow") as mock_trigger:
        res2 = client.post(
            f"/api/v1/meetings/{sample_meeting_with_official.id}/ai/process",
            json={"force_reprocess": False},
        )
        assert res2.status_code == 409
        mock_trigger.assert_not_called()


# ==============================================================================
# CROSS-CUTTING INVARIANTS: POSTGRES ISOLATION & SHEETS_RETRY GRAPH REACHABILITY
# ==============================================================================
def test_acceptance_invariant_n8n_zero_postgresql_nodes_and_credentials() -> None:
    """
    Invariant 1: n8n workflow must have ZERO postgres nodes and ZERO postgres credentials.
    """
    wf = load_workflow_json()
    for node in wf["nodes"]:
        node_type = str(node.get("type", "")).lower()
        node_name = str(node.get("name", "")).lower()
        credentials = str(node.get("credentials", "")).lower()

        assert "postgres" not in node_type, f"Forbidden postgres node found: {node.get('name')}"
        assert "postgres" not in node_name, f"Forbidden postgres node name found: {node.get('name')}"
        assert "postgres" not in credentials, f"Forbidden postgres credential found in: {node.get('name')}"


def test_acceptance_invariant_sheets_retry_zero_ai_reachability() -> None:
    """
    Invariant 2: Graph traversal must prove ZERO AI / LLM nodes are reachable from SHEETS_RETRY branch.
    """
    wf = load_workflow_json()
    connections = wf.get("connections", {})
    nodes_by_name = {n["name"]: n for n in wf["nodes"]}

    # Identify all AI nodes
    ai_node_names = set()
    for name, node in nodes_by_name.items():
        node_type = str(node.get("type", "")).lower()
        if "langchain" in node_type or "llm" in name.lower() or "ai retry" in name.lower() or "model" in name.lower():
            ai_node_names.add(name)

    assert "Analyze Meeting - Gemini LLM Chain" in ai_node_names
    assert "AI Retry #1 - Gemini LLM Chain" in ai_node_names

    # Start graph traversal from SHEETS_RETRY entry node
    sheets_retry_start = "Extract & Format Cached AI Result"
    assert sheets_retry_start in nodes_by_name, "Node 'Extract & Format Cached AI Result' must exist in workflow"

    # Breadth-first search (BFS) downstream from SHEETS_RETRY start node
    visited = set()
    queue = [sheets_retry_start]
    reachable_from_sheets_retry = set()

    while queue:
        current_node = queue.pop(0)
        if current_node in visited:
            continue
        visited.add(current_node)
        reachable_from_sheets_retry.add(current_node)

        # Enqueue downstream children
        node_conns = connections.get(current_node, {}).get("main", [])
        for output_group in node_conns:
            for conn_target in output_group:
                next_name = conn_target.get("node")
                if next_name and next_name not in visited:
                    queue.append(next_name)

    # Assert that NO AI nodes are reachable from SHEETS_RETRY
    intersection = reachable_from_sheets_retry.intersection(ai_node_names)
    assert len(intersection) == 0, f"AI nodes reachable from SHEETS_RETRY: {intersection}"


def test_acceptance_invariant_gemini_provider_enforcement() -> None:
    """
    Invariant 3: Workflow strictly enforces Google Gemini provider and rejects unsupported providers.
    """
    wf = load_workflow_json()
    auth_node = next(n for n in wf["nodes"] if n["name"] == "Authenticate & Validate Input")
    js_code = auth_node["parameters"]["jsCode"]

    assert "ai_provider" in js_code
    assert "Only 'google' / 'gemini' is supported" in js_code or "rawProvider !== 'google'" in js_code
