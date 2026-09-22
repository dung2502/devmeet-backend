import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.integrations.google_meet import GoogleMeetListResponse
from app.main import app
from app.models import Meeting, Participant, Transcript, TranscriptEntry, User
from app.repositories import MeetingRepository, UserRepository


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    user_repo = UserRepository(db_session)
    user = user_repo.create(
        {
            "google_user_id": "test-api-user-12345",
            "email": "api-test@example.com",
            "display_name": "API Test User",
        }
    )

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_get_current_user():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health_endpoint_regression(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_openapi_schema_endpoint_registration(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]

    # Verify registered Phase 2 endpoints
    assert "/api/v1/health" in paths
    assert "/api/v1/meetings/sync" in paths
    assert "/api/v1/meetings/{meeting_id}" in paths
    assert "/api/v1/meetings/{meeting_id}/participants/sync" in paths
    assert "/api/v1/meetings/{meeting_id}/participants" in paths
    assert "/api/v1/meetings/{meeting_id}/transcript/sync" in paths
    assert "/api/v1/meetings/{meeting_id}/transcript" in paths
    assert "/api/v1/meetings/{meeting_id}/transcript/status" in paths
    assert "/api/v1/meetings/{meeting_id}/transcript/entries" in paths

    # Verify Phase 4E endpoints
    assert "/api/v1/meetings/{meeting_id}/ai/process" in paths
    assert "/api/v1/meetings/{meeting_id}/ai/status" in paths
    assert "/api/v1/meetings/{meeting_id}/comparison" not in paths


def test_meeting_sync_and_get_meeting(client: TestClient, db_session: Session) -> None:
    # 1. Sync meeting without Google token (metadata sync)
    sync_payload = {
        "conference_record_name": "conferenceRecords/conf-api-101",
        "meeting_space_name": "spaces/space-api-101",
        "title": "API Test Meeting",
    }
    res = client.post("/api/v1/meetings/sync", json=sync_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["conference_record_name"] == "conferenceRecords/conf-api-101"
    assert data["title"] == "API Test Meeting"
    meeting_id = data["id"]

    # 2. Idempotent sync call
    res_2 = client.post("/api/v1/meetings/sync", json=sync_payload)
    assert res_2.status_code == 200
    assert res_2.json()["id"] == meeting_id

    # 3. GET meeting
    res_get = client.get(f"/api/v1/meetings/{meeting_id}")
    assert res_get.status_code == 200
    assert res_get.json()["id"] == meeting_id


def test_participant_sync_and_get_participants(
    client: TestClient, db_session: Session
) -> None:
    # Setup meeting
    user = UserRepository(db_session).create(
        {"google_user_id": "u-part", "email": "part@example.com"}
    )
    meeting = MeetingRepository(db_session).create(
        {
            "user_id": user.id,
            "conference_record_name": "conferenceRecords/conf-part-sync",
        }
    )

    # Missing token -> 401
    res_no_token = client.post(f"/api/v1/meetings/{meeting.id}/participants/sync")
    assert res_no_token.status_code == 401
    assert res_no_token.json()["error"]["code"] == "GOOGLE_AUTH_FAILED"

    # Mock Google API call
    mock_resp = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-part-sync/participants/p1",
                "signedinUser": {"displayName": "Alice API", "email": "alice.api@example.com"},
            }
        ],
        next_page_token=None,
        raw={},
    )

    with patch(
        "app.integrations.google_meet.GoogleMeetClient.list_participants",
        return_value=mock_resp,
    ):
        res_sync = client.post(
            f"/api/v1/meetings/{meeting.id}/participants/sync",
            headers={"X-Google-Access-Token": "valid-token-123"},
        )
        assert res_sync.status_code == 200
        sync_data = res_sync.json()
        assert sync_data["participants_synced"] == 1
        assert sync_data["items"][0]["display_name"] == "Alice API"

    # GET participants endpoint
    res_get = client.get(f"/api/v1/meetings/{meeting.id}/participants")
    assert res_get.status_code == 200
    parts = res_get.json()
    assert len(parts) == 1
    assert parts[0]["display_name"] == "Alice API"


def test_transcript_sync_started_returns_transcript_not_ready(
    client: TestClient, db_session: Session
) -> None:
    user = UserRepository(db_session).create(
        {"google_user_id": "u-tx-start", "email": "txstart@example.com"}
    )
    meeting = MeetingRepository(db_session).create(
        {
            "user_id": user.id,
            "conference_record_name": "conferenceRecords/conf-tx-start",
        }
    )

    mock_resp = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-tx-start/transcripts/t1",
                "state": "STARTED",
            }
        ],
        next_page_token=None,
        raw={},
    )

    with patch(
        "app.integrations.google_meet.GoogleMeetClient.list_transcripts",
        return_value=mock_resp,
    ):
        res_sync = client.post(
            f"/api/v1/meetings/{meeting.id}/transcript/sync",
            headers={"X-Google-Access-Token": "valid-token-123"},
        )
        assert res_sync.status_code == 400
        err = res_sync.json()["error"]
        assert err["code"] == "TRANSCRIPT_NOT_READY"


def test_transcript_sync_success_and_get_endpoints(
    client: TestClient, db_session: Session
) -> None:
    user = UserRepository(db_session).create(
        {"google_user_id": "u-tx-succ", "email": "txsucc@example.com"}
    )
    meeting = MeetingRepository(db_session).create(
        {
            "user_id": user.id,
            "conference_record_name": "conferenceRecords/conf-tx-succ",
        }
    )

    mock_tx_resp = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-tx-succ/transcripts/t1",
                "state": "FILE_GENERATED",
            }
        ],
        next_page_token=None,
        raw={},
    )

    mock_entry_resp = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-tx-succ/transcripts/t1/entries/e1",
                "text": "API transcript entry statement.",
                "languageCode": "en-US",
            }
        ],
        next_page_token=None,
        raw={},
    )

    with patch(
        "app.integrations.google_meet.GoogleMeetClient.list_transcripts",
        return_value=mock_tx_resp,
    ), patch(
        "app.integrations.google_meet.GoogleMeetClient.list_transcript_entries",
        return_value=mock_entry_resp,
    ):
        res_sync = client.post(
            f"/api/v1/meetings/{meeting.id}/transcript/sync",
            headers={"X-Google-Access-Token": "valid-token-123"},
        )
        assert res_sync.status_code == 200
        data = res_sync.json()
        assert data["status"] == "available"
        assert data["entries_synced"] == 1

    # GET transcript metadata
    res_tx = client.get(f"/api/v1/meetings/{meeting.id}/transcript")
    assert res_tx.status_code == 200
    assert res_tx.json()["state"] == "FILE_GENERATED"

    # GET transcript status
    res_status = client.get(f"/api/v1/meetings/{meeting.id}/transcript/status")
    assert res_status.status_code == 200
    assert res_status.json()["entries_count"] == 1

    # GET transcript entries
    res_entries = client.get(f"/api/v1/meetings/{meeting.id}/transcript/entries")
    assert res_entries.status_code == 200
    entries_data = res_entries.json()
    assert entries_data["total"] == 1
    assert entries_data["items"][0]["text"] == "API transcript entry statement."


def test_non_existent_meeting_404(client: TestClient) -> None:
    bad_id = uuid.uuid4()
    res = client.get(f"/api/v1/meetings/{bad_id}")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "MEETING_NOT_FOUND"
