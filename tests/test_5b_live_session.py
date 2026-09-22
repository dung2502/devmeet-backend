import uuid
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import GoogleIdentity, GoogleIdentityResolver, get_current_user
from app.database import get_db
from app.main import app
from app.models import LiveSession, LiveSessionStatus, Meeting, User
from app.repositories import MeetingRepository, UserRepository


@pytest.fixture()
def user_a(db_session: Session) -> User:
    user_repo = UserRepository(db_session)
    return user_repo.create(
        {
            "google_user_id": "google-user-a-111",
            "email": "user_a@example.com",
            "display_name": "User A (Host)",
        }
    )


@pytest.fixture()
def user_b(db_session: Session) -> User:
    user_repo = UserRepository(db_session)
    return user_repo.create(
        {
            "google_user_id": "google-user-b-222",
            "email": "user_b@example.com",
            "display_name": "User B (Participant)",
        }
    )


@pytest.fixture()
def test_meeting(db_session: Session, user_a: User) -> Meeting:
    meeting_repo = MeetingRepository(db_session)
    return meeting_repo.create_extension_meeting(
        {
            "user_id": user_a.id,
            "conference_record_name": None,
            "meeting_url": "https://meet.google.com/live-session-test-room",
            "title": "Live Session Test Meeting",
        }
    )


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


def test_c1_create_new_live_session_returns_201_active_and_uuid_session_id(
    client: TestClient, test_meeting: Meeting, user_a: User
) -> None:
    app.dependency_overrides[get_current_user] = lambda: user_a
    tab_uuid = uuid.uuid4()
    payload = {
        "meeting_id": str(test_meeting.id),
        "tab_session_uuid": str(tab_uuid),
    }
    res = client.post("/api/v1/live-sessions", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["meeting_id"] == str(test_meeting.id)
    assert data["user_id"] == str(user_a.id)
    assert data["tab_session_uuid"] == str(tab_uuid)
    assert data["status"] == LiveSessionStatus.ACTIVE.value
    assert "session_id" in data
    assert str(uuid.UUID(data["session_id"])) == data["session_id"]


def test_c2_idempotent_same_tuple_returns_200_same_session_id_no_new_row(
    client: TestClient, test_meeting: Meeting, user_a: User, db_session: Session
) -> None:
    app.dependency_overrides[get_current_user] = lambda: user_a
    tab_uuid = uuid.uuid4()
    payload = {
        "meeting_id": str(test_meeting.id),
        "tab_session_uuid": str(tab_uuid),
    }
    res1 = client.post("/api/v1/live-sessions", json=payload)
    assert res1.status_code == 201
    session_id_1 = res1.json()["session_id"]

    res2 = client.post("/api/v1/live-sessions", json=payload)
    assert res2.status_code == 200
    session_id_2 = res2.json()["session_id"]
    assert session_id_1 == session_id_2

    total_sessions = db_session.query(LiveSession).count()
    assert total_sessions == 1


def test_c3_different_tab_same_user_supersedes_old_active_to_disconnected(
    client: TestClient, test_meeting: Meeting, user_a: User, db_session: Session
) -> None:
    app.dependency_overrides[get_current_user] = lambda: user_a
    tab1 = uuid.uuid4()
    tab2 = uuid.uuid4()

    res1 = client.post(
        "/api/v1/live-sessions",
        json={"meeting_id": str(test_meeting.id), "tab_session_uuid": str(tab1)},
    )
    assert res1.status_code == 201
    session_id_1 = uuid.UUID(res1.json()["session_id"])

    res2 = client.post(
        "/api/v1/live-sessions",
        json={"meeting_id": str(test_meeting.id), "tab_session_uuid": str(tab2)},
    )
    assert res2.status_code == 201
    session_id_2 = uuid.UUID(res2.json()["session_id"])
    assert session_id_1 != session_id_2

    db_session.expire_all()
    s1 = db_session.get(LiveSession, session_id_1)
    s2 = db_session.get(LiveSession, session_id_2)
    assert s1.status == LiveSessionStatus.DISCONNECTED.value
    assert s2.status == LiveSessionStatus.ACTIVE.value


def test_c4_only_one_active_session_exists_for_same_user_and_meeting(
    client: TestClient, test_meeting: Meeting, user_a: User, db_session: Session
) -> None:
    app.dependency_overrides[get_current_user] = lambda: user_a
    for _ in range(3):
        client.post(
            "/api/v1/live-sessions",
            json={"meeting_id": str(test_meeting.id), "tab_session_uuid": str(uuid.uuid4())},
        )

    db_session.expire_all()
    active_sessions = (
        db_session.query(LiveSession)
        .filter(
            LiveSession.user_id == user_a.id,
            LiveSession.meeting_id == test_meeting.id,
            LiveSession.status == LiveSessionStatus.ACTIVE.value,
        )
        .all()
    )
    assert len(active_sessions) == 1


def test_c5_different_users_on_same_meeting_both_have_active_sessions(
    client: TestClient,
    test_meeting: Meeting,
    user_a: User,
    user_b: User,
    db_session: Session,
) -> None:
    def fake_resolve(token):
        if token == "token_a":
            return GoogleIdentity(google_user_id=user_a.google_user_id, email=user_a.email, display_name=user_a.display_name)
        elif token == "token_b":
            return GoogleIdentity(google_user_id=user_b.google_user_id, email=user_b.email, display_name=user_b.display_name)
        raise AuthenticationError("Invalid token")

    if get_current_user in app.dependency_overrides:
        del app.dependency_overrides[get_current_user]

    with patch.object(GoogleIdentityResolver, "resolve_access_token", side_effect=fake_resolve):
        res_a = client.post(
            "/api/v1/live-sessions",
            headers={"Authorization": "Bearer token_a"},
            json={"meeting_id": str(test_meeting.id), "tab_session_uuid": str(uuid.uuid4())},
        )
        res_b = client.post(
            "/api/v1/live-sessions",
            headers={"Authorization": "Bearer token_b"},
            json={"meeting_id": str(test_meeting.id), "tab_session_uuid": str(uuid.uuid4())},
        )
        assert res_a.status_code == 201
        assert res_b.status_code == 201

        db_session.expire_all()
        s_a = db_session.get(LiveSession, uuid.UUID(res_a.json()["session_id"]))
        s_b = db_session.get(LiveSession, uuid.UUID(res_b.json()["session_id"]))
        assert s_a.status == LiveSessionStatus.ACTIVE.value
        assert s_b.status == LiveSessionStatus.ACTIVE.value


def test_c6_invalid_meeting_id_returns_404(
    client: TestClient, user_a: User
) -> None:
    app.dependency_overrides[get_current_user] = lambda: user_a
    non_existent = uuid.uuid4()
    res = client.post(
        "/api/v1/live-sessions",
        json={"meeting_id": str(non_existent), "tab_session_uuid": str(uuid.uuid4())},
    )
    assert res.status_code == 404
    data = res.json()
    assert data["error"]["code"] == "MEETING_NOT_FOUND"


def test_c7_mandatory_participant_scenario_user_b_on_user_a_meeting(
    client: TestClient, test_meeting: Meeting, user_a: User, user_b: User
) -> None:
    assert test_meeting.user_id == user_a.id
    app.dependency_overrides[get_current_user] = lambda: user_b

    res = client.post(
        "/api/v1/live-sessions",
        json={"meeting_id": str(test_meeting.id), "tab_session_uuid": str(uuid.uuid4())},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["meeting_id"] == str(test_meeting.id)
    assert data["user_id"] == str(user_b.id)
    assert data["status"] == LiveSessionStatus.ACTIVE.value


def test_c8_client_provided_user_id_is_ignored_and_uses_authenticated_user(
    client: TestClient, test_meeting: Meeting, user_a: User
) -> None:
    app.dependency_overrides[get_current_user] = lambda: user_a
    fake_user = uuid.uuid4()
    payload = {
        "meeting_id": str(test_meeting.id),
        "tab_session_uuid": str(uuid.uuid4()),
        "user_id": str(fake_user),
    }
    res = client.post("/api/v1/live-sessions", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["user_id"] == str(user_a.id)
    assert data["user_id"] != str(fake_user)


def test_c9_no_status_superseded(
    client: TestClient, test_meeting: Meeting, user_a: User, db_session: Session
) -> None:
    app.dependency_overrides[get_current_user] = lambda: user_a
    client.post(
        "/api/v1/live-sessions",
        json={"meeting_id": str(test_meeting.id), "tab_session_uuid": str(uuid.uuid4())},
    )
    client.post(
        "/api/v1/live-sessions",
        json={"meeting_id": str(test_meeting.id), "tab_session_uuid": str(uuid.uuid4())},
    )
    db_session.expire_all()
    statuses = [s.status for s in db_session.query(LiveSession).all()]
    assert "SUPERSEDED" not in statuses


def test_c10_only_canonical_status_values(
    client: TestClient, test_meeting: Meeting, user_a: User, db_session: Session
) -> None:
    app.dependency_overrides[get_current_user] = lambda: user_a
    allowed = {"INIT", "ACTIVE", "PAUSED", "DISCONNECTED", "COMPLETED"}
    client.post(
        "/api/v1/live-sessions",
        json={"meeting_id": str(test_meeting.id), "tab_session_uuid": str(uuid.uuid4())},
    )
    db_session.expire_all()
    for s in db_session.query(LiveSession).all():
        assert s.status in allowed
