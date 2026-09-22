import uuid
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models import Meeting, User
from app.repositories import MeetingRepository, UserRepository


@pytest.fixture()
def auth_user(db_session: Session) -> User:
    user_repo = UserRepository(db_session)
    return user_repo.create(
        {
            "google_user_id": "google-user-meeting-5b-01",
            "email": "user5b_meeting@example.com",
            "display_name": "Meeting Test User",
        }
    )


@pytest.fixture()
def client(db_session: Session, auth_user: User) -> TestClient:
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_get_current_user():
        return auth_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_a1_extension_first_meeting_sync_without_conference_record_name(
    client: TestClient, db_session: Session, auth_user: User
) -> None:
    payload = {
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "title": "Extension Standup Meeting",
        "conference_record_name": None,
    }
    response = client.post("/api/v1/meetings/sync", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    assert data["title"] == "Extension Standup Meeting"
    assert data["conference_record_name"] is None


def test_a2_created_meeting_has_uuid_id(
    client: TestClient
) -> None:
    payload = {
        "meeting_url": "https://meet.google.com/xyz-uvw-123",
        "title": "UUID Test",
    }
    response = client.post("/api/v1/meetings/sync", json=payload)
    assert response.status_code == 200
    data = response.json()
    parsed_uuid = uuid.UUID(data["id"])
    assert str(parsed_uuid) == data["id"]


def test_a3_created_meeting_user_id_is_authenticated_devmeet_user(
    client: TestClient, auth_user: User
) -> None:
    payload = {
        "meeting_url": "https://meet.google.com/auth-user-check",
        "title": "Auth User ID Check",
    }
    response = client.post("/api/v1/meetings/sync", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == str(auth_user.id)


def test_a4_same_meeting_url_creates_distinct_meeting_occurrences(
    client: TestClient, db_session: Session
) -> None:
    payload1 = {
        "meeting_url": "https://meet.google.com/recurring-room-01",
        "title": "Daily Standup Day 1",
    }
    payload2 = {
        "meeting_url": "https://meet.google.com/recurring-room-01",
        "title": "Daily Standup Day 1 - Join",
    }
    # Within active in_progress window: resolves to same shared room
    res1 = client.post("/api/v1/meetings/sync", json=payload1)
    res2 = client.post("/api/v1/meetings/sync", json=payload2)
    assert res1.status_code == 200
    assert res2.status_code == 200

    id1 = res1.json()["id"]
    id2 = res2.json()["id"]
    assert id1 == id2

    # After first occurrence completes: next day creates distinct occurrence
    meeting_repo = MeetingRepository(db_session)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    meeting_repo.update(uuid.UUID(id1), {
        "status": "completed",
        "last_heartbeat_at": yesterday,
        "end_time": yesterday,
    })

    payload3 = {
        "meeting_url": "https://meet.google.com/recurring-room-01",
        "title": "Daily Standup Day 2",
    }
    res3 = client.post("/api/v1/meetings/sync", json=payload3)
    assert res3.status_code == 200
    id3 = res3.json()["id"]
    assert id3 != id1



def test_a5_no_uniqueness_requirement_on_user_id_and_meeting_url(
    db_session: Session, auth_user: User
) -> None:
    meeting_repo = MeetingRepository(db_session)
    url = "https://meet.google.com/no-unique-constraint-url"
    m1 = meeting_repo.create_extension_meeting(
        {
            "user_id": auth_user.id,
            "conference_record_name": None,
            "meeting_url": url,
            "title": "Session 1",
        }
    )
    m2 = meeting_repo.create_extension_meeting(
        {
            "user_id": auth_user.id,
            "conference_record_name": None,
            "meeting_url": url,
            "title": "Session 2",
        }
    )
    assert m1.id != m2.id
    assert m1.meeting_url == m2.meeting_url
    assert m1.user_id == m2.user_id


def test_a6_extension_first_meeting_does_not_fabricate_conference_record_name(
    client: TestClient
) -> None:
    payload = {
        "meeting_url": "https://meet.google.com/no-fabrication-test",
        "title": "No Fake ID",
    }
    response = client.post("/api/v1/meetings/sync", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["conference_record_name"] is None


def test_a7_existing_meeting_id_resolves_existing_meeting(
    client: TestClient, db_session: Session, auth_user: User
) -> None:
    meeting_repo = MeetingRepository(db_session)
    initial_meeting = meeting_repo.create_extension_meeting(
        {
            "user_id": auth_user.id,
            "conference_record_name": None,
            "meeting_url": "https://meet.google.com/tracked-meeting-1",
            "title": "Initial Title",
        }
    )

    resolve_payload = {
        "meeting_id": str(initial_meeting.id),
        "title": "Updated Title",
    }
    response = client.post("/api/v1/meetings/sync", json=resolve_payload)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(initial_meeting.id)
    assert data["title"] == "Updated Title"
