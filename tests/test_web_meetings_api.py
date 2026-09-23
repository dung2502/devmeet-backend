import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models import Meeting, Participant, User
from app.repositories import MeetingRepository, UserRepository


@pytest.fixture()
def auth_setup(db_session: Session):
    user_repo = UserRepository(db_session)
    user_a = user_repo.create(
        {
            "google_user_id": "google-user-a-111",
            "email": "user_a@example.com",
            "display_name": "User Alpha",
        }
    )
    user_b = user_repo.create(
        {
            "google_user_id": "google-user-b-222",
            "email": "user_b@example.com",
            "display_name": "User Beta",
        }
    )

    current_active_user = [user_a]

    def override_get_db():
        yield db_session

    def override_get_current_user():
        return current_active_user[0]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as client:
        yield {
            "client": client,
            "user_a": user_a,
            "user_b": user_b,
            "set_user": lambda u: current_active_user.__setitem__(0, u),
            "session": db_session,
        }

    app.dependency_overrides.clear()


def test_list_meetings_empty_and_user_isolation(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    user_b = auth_setup["user_b"]
    session = auth_setup["session"]

    # User A has 0 meetings initially
    res = client.get("/api/v1/meetings")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 0
    assert data["items"] == []

    # Create meeting for User B
    m_repo = MeetingRepository(session)
    m_b = m_repo.create(
        {
            "user_id": user_b.id,
            "title": "Secret Meeting of User B",
            "status": "completed",
            "start_time": datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc),
        }
    )

    # User A must NOT see User B's meeting
    res_a = client.get("/api/v1/meetings")
    assert res_a.status_code == 200
    assert res_a.json()["total"] == 0

    # User A trying to GET /meetings/{m_b.id} directly gets 404 (isolation)
    res_get_a = client.get(f"/api/v1/meetings/{m_b.id}")
    assert res_get_a.status_code == 404

    # Switch to User B -> sees 1 meeting
    auth_setup["set_user"](user_b)
    res_b = client.get("/api/v1/meetings")
    assert res_b.status_code == 200
    data_b = res_b.json()
    assert data_b["total"] == 1
    assert data_b["items"][0]["title"] == "Secret Meeting of User B"

    # User B can get detail
    res_get_b = client.get(f"/api/v1/meetings/{m_b.id}")
    assert res_get_b.status_code == 200
    assert res_get_b.json()["title"] == "Secret Meeting of User B"


def test_list_meetings_sorting_and_search(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    session = auth_setup["session"]
    m_repo = MeetingRepository(session)

    # Create 3 meetings with different dates and titles
    m1 = m_repo.create(
        {
            "user_id": user_a.id,
            "title": "Sprint Planning Q3",
            "start_time": datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc),
        }
    )
    m2 = m_repo.create(
        {
            "user_id": user_a.id,
            "title": "Daily Standup Backend",
            "start_time": datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc),
        }
    )
    m3 = m_repo.create(
        {
            "user_id": user_a.id,
            "title": "Architecture Review n8n",
            "start_time": datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc),
        }
    )

    # Default sort start_time_desc: m2 (Sep 15), m3 (Sep 12), m1 (Sep 10)
    res = client.get("/api/v1/meetings?sort=start_time_desc")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["id"] == str(m2.id)
    assert items[1]["id"] == str(m3.id)
    assert items[2]["id"] == str(m1.id)

    # Sort start_time_asc: m1 (Sep 10), m3 (Sep 12), m2 (Sep 15)
    res_asc = client.get("/api/v1/meetings?sort=start_time_asc")
    assert res_asc.status_code == 200
    items_asc = res_asc.json()["items"]
    assert items_asc[0]["id"] == str(m1.id)
    assert items_asc[2]["id"] == str(m2.id)

    # Search keyword "Standup"
    res_search = client.get("/api/v1/meetings?search=Standup")
    assert res_search.status_code == 200
    search_data = res_search.json()
    assert search_data["total"] == 1
    assert search_data["items"][0]["title"] == "Daily Standup Backend"


def test_get_meeting_detail_with_participants_and_sheets_url(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    session = auth_setup["session"]
    m_repo = MeetingRepository(session)

    m = m_repo.create(
        {
            "user_id": user_a.id,
            "title": "Demo Meeting",
            "status": "completed",
            "sheets_sync_status": "SYNCED",
            "ai_status": "COMPLETED",
            "ai_result": {"summary": "Great meeting."},
        }
    )

    p1 = Participant(
        meeting_id=m.id,
        google_participant_name="spaces/space1/participants/p1",
        display_name="Nguyen Van A",
        email="a@example.com",
    )
    session.add(p1)
    session.commit()

    res = client.get(f"/api/v1/meetings/{m.id}")
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == str(m.id)
    assert data["title"] == "Demo Meeting"
    assert len(data["participants"]) == 1
    assert data["participants"][0]["display_name"] == "Nguyen Van A"
    assert data["ai_status"] == "COMPLETED"
    assert data["sheets_sync_status"] == "SYNCED"


def test_get_meeting_detail_with_dom_speakers_resolution(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    session = auth_setup["session"]
    m_repo = MeetingRepository(session)

    m = m_repo.create(
        {
            "user_id": user_a.id,
            "title": "DOM Captions Only Meeting",
            "status": "completed",
            "dom_capture_status": "received",
        }
    )

    from app.models.live_session import LiveSession
    from app.models.meeting_dom_segment import MeetingDOMSegment

    ls = LiveSession(
        session_id=uuid.uuid4(),
        meeting_id=m.id,
        user_id=user_a.id,
        tab_session_uuid=uuid.uuid4(),
        status="completed",
    )
    session.add(ls)
    session.commit()

    seg = MeetingDOMSegment(
        segment_id=uuid.uuid4(),
        meeting_id=m.id,
        session_id=ls.session_id,
        sequence=1,
        speaker_name="Mintesnot Udessa",
        text="Lúc nãy. Có.",
        observed_start_epoch_ms=1000,
        observed_end_epoch_ms=2000,
    )
    session.add(seg)
    session.commit()

    # Verify get detail resolves the DOM speaker as participant
    res = client.get(f"/api/v1/meetings/{m.id}")
    assert res.status_code == 200
    data = res.json()
    assert len(data["participants"]) == 1
    assert data["participants"][0]["display_name"] == "Mintesnot Udessa"

    # Verify list meetings counts this participant
    res_list = client.get("/api/v1/meetings")
    assert res_list.status_code == 200
    item = next(it for it in res_list.json()["items"] if it["id"] == str(m.id))
    assert item["participant_count"] == 1


def test_update_meeting_title_rbac(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    user_b = auth_setup["user_b"]
    session = auth_setup["session"]
    m_repo = MeetingRepository(session)

    # User A creates meeting (owner)
    m = m_repo.create(
        {
            "user_id": user_a.id,
            "title": "Old Standup Title",
            "status": "completed",
        }
    )

    # 1. User A (Owner) can rename meeting
    res_update = client.patch(
        f"/api/v1/meetings/{m.id}",
        json={"title": "New Sprint Retrospective"},
    )
    assert res_update.status_code == 200
    assert res_update.json()["title"] == "New Sprint Retrospective"

    # 2. Empty title validation fails
    res_empty = client.patch(
        f"/api/v1/meetings/{m.id}",
        json={"title": "   "},
    )
    assert res_empty.status_code == 400

    # 3. User B (Participant / Non-Owner) is forbidden from renaming
    auth_setup["set_user"](user_b)
    from app.repositories.meeting_access_repository import MeetingAccessRepository
    access_repo = MeetingAccessRepository(session)
    access_repo.add_or_update_access(m.id, user_b.id, role="PARTICIPANT")
    session.commit()

    res_forbidden = client.patch(
        f"/api/v1/meetings/{m.id}",
        json={"title": "Hacked Title By Participant"},
    )
    assert res_forbidden.status_code == 403
    assert "Chỉ chủ phòng" in res_forbidden.json()["detail"]


