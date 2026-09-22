import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models import Meeting, MeetingAccess, User
from app.models.live_session import LiveSession, LiveSessionStatus
from app.repositories import MeetingAccessRepository, MeetingRepository, UserRepository
from app.services.conference_identity_resolver import ConferenceIdentityResolver
from app.services.meeting_lifecycle_service import MeetingLifecycleService


@pytest.fixture()
def shared_room_setup(db_session: Session):
    user_repo = UserRepository(db_session)
    dung = user_repo.create(
        {
            "google_user_id": "google-dung-123",
            "email": "dung@example.com",
            "display_name": "Dung Nguyen",
        }
    )
    mintesnot = user_repo.create(
        {
            "google_user_id": "google-mintesnot-456",
            "email": "mintesnot@example.com",
            "display_name": "Mintesnot Udessa",
        }
    )

    current_active_user = [dung]

    def override_get_db():
        yield db_session

    def override_get_current_user():
        return current_active_user[0]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as client:
        yield {
            "client": client,
            "dung": dung,
            "mintesnot": mintesnot,
            "set_user": lambda u: current_active_user.__setitem__(0, u),
            "session": db_session,
        }

    app.dependency_overrides.clear()


def test_shared_room_resolution_and_access(shared_room_setup):
    client = shared_room_setup["client"]
    dung = shared_room_setup["dung"]
    mintesnot = shared_room_setup["mintesnot"]
    set_user = shared_room_setup["set_user"]
    session = shared_room_setup["session"]

    # 1. Dung (Host) creates / syncs room
    set_user(dung)
    res_dung = client.post(
        "/api/v1/meetings/sync",
        json={
            "meeting_url": "https://meet.google.com/kpz-znea-bwr",
            "title": "Sprint Review",
        },
    )
    assert res_dung.status_code == 200
    meeting_id = res_dung.json()["id"]

    # Verify Dung has the meeting on Dashboard with role OWNER
    res_list_dung = client.get("/api/v1/meetings")
    assert res_list_dung.status_code == 200
    items_dung = res_list_dung.json()["items"]
    assert len(items_dung) == 1
    assert items_dung[0]["id"] == meeting_id
    assert items_dung[0]["user_role"] == "OWNER"

    # 2. Mintesnot (Attendee) syncs the SAME room with their extension
    set_user(mintesnot)
    res_mintesnot = client.post(
        "/api/v1/meetings/sync",
        json={
            "meeting_url": "https://meet.google.com/kpz-znea-bwr",
        },
    )
    assert res_mintesnot.status_code == 200
    # Must resolve to the EXACT SAME meeting_id!
    assert res_mintesnot.json()["id"] == meeting_id

    # 3. Verify Mintesnot now SEES the meeting on their Dashboard with role PARTICIPANT
    res_list_mintesnot = client.get("/api/v1/meetings")
    assert res_list_mintesnot.status_code == 200
    items_mintesnot = res_list_mintesnot.json()["items"]
    assert len(items_mintesnot) == 1
    assert items_mintesnot[0]["id"] == meeting_id
    assert items_mintesnot[0]["user_role"] == "PARTICIPANT"
    assert items_mintesnot[0]["host_name"] == "Dung Nguyen"


def test_continuity_window_and_partial_unique_index(shared_room_setup):
    session = shared_room_setup["session"]
    dung = shared_room_setup["dung"]
    mintesnot = shared_room_setup["mintesnot"]

    resolver = ConferenceIdentityResolver(session)
    now = datetime.now(timezone.utc)

    # Day 1: Dung creates room
    m1, is_created_1 = resolver.resolve_or_create_shared_meeting(
        user_id=dung.id,
        meeting_url="https://meet.google.com/fixed-room-abc",
        title="Sprint Standup Day 1",
    )
    assert is_created_1 is True

    # Simulate Day 2: 24 hours later (continuity window expired > 30 mins)
    # The old meeting is still in_progress in DB (e.g. abrupt disconnect)
    m1.created_at = now - timedelta(hours=24)
    m1.last_heartbeat_at = now - timedelta(hours=24)
    session.commit()

    # When Mintesnot or Dung enters the room next day, resolver must:
    # 1. Close old meeting to 'completed'
    # 2. Insert new meeting with status='in_progress' WITHOUT Partial Unique Index conflict!
    m2, is_created_2 = resolver.resolve_or_create_shared_meeting(
        user_id=mintesnot.id,
        meeting_url="https://meet.google.com/fixed-room-abc",
        title="Sprint Standup Day 2",
    )
    assert is_created_2 is True
    assert m2.id != m1.id
    assert m2.status == "in_progress"

    # Verify old meeting m1 was transitioned to completed
    session.refresh(m1)
    assert m1.status == "completed"


def test_meeting_delete_in_progress_guard_and_soft_remove(shared_room_setup):
    client = shared_room_setup["client"]
    dung = shared_room_setup["dung"]
    mintesnot = shared_room_setup["mintesnot"]
    set_user = shared_room_setup["set_user"]
    session = shared_room_setup["session"]

    set_user(dung)
    res = client.post(
        "/api/v1/meetings/sync",
        json={"meeting_url": "https://meet.google.com/active-meeting-xyz"},
    )
    meeting_id = res.json()["id"]

    # Add Mintesnot as participant
    set_user(mintesnot)
    client.post(
        "/api/v1/meetings/sync",
        json={"meeting_url": "https://meet.google.com/active-meeting-xyz"},
    )

    # 1. Dung (OWNER) and Mintesnot (PARTICIPANT) cannot delete while in_progress -> HTTP 400
    set_user(dung)
    res_del_in_progress = client.delete(f"/api/v1/meetings/{meeting_id}")
    assert res_del_in_progress.status_code == 400
    assert "Không thể xóa cuộc họp đang diễn ra" in res_del_in_progress.json()["detail"]

    set_user(mintesnot)
    res_del_in_progress_m = client.delete(f"/api/v1/meetings/{meeting_id}")
    assert res_del_in_progress_m.status_code == 400
    assert "Không thể xóa cuộc họp đang diễn ra" in res_del_in_progress_m.json()["detail"]

    # Mark meeting completed
    m = session.get(Meeting, uuid.UUID(meeting_id))
    m.status = "completed"
    session.commit()

    # 2. Dung (Creator / OWNER) deletes first -> Soft Remove & Ownership transfer to Mintesnot!
    set_user(dung)
    res_dung_del = client.delete(f"/api/v1/meetings/{meeting_id}")
    assert res_dung_del.status_code == 200
    assert res_dung_del.json()["action"] == "soft_remove"
    assert res_dung_del.json()["remaining_users_count"] == 1

    # Meeting is gone from Dung's dashboard
    res_list_d = client.get("/api/v1/meetings")
    assert len(res_list_d.json()["items"]) == 0

    # Meeting remains 100% intact on Mintesnot's dashboard, and Mintesnot is promoted to OWNER
    set_user(mintesnot)
    res_list_m = client.get("/api/v1/meetings")
    assert len(res_list_m.json()["items"]) == 1
    assert res_list_m.json()["items"][0]["id"] == meeting_id
    assert res_list_m.json()["items"][0]["user_role"] == "OWNER"

    # 3. Mintesnot (now the last remaining user) deletes -> Hard delete
    res_mintesnot_del = client.delete(f"/api/v1/meetings/{meeting_id}")
    assert res_mintesnot_del.status_code == 200
    assert res_mintesnot_del.json()["action"] == "hard_delete"
    assert res_mintesnot_del.json()["remaining_users_count"] == 0

    # Meeting is completely removed from DB
    session.expire_all()
    assert session.get(Meeting, uuid.UUID(meeting_id)) is None


def test_ai_participant_guard_and_deadlock_prevention(shared_room_setup):
    client = shared_room_setup["client"]
    dung = shared_room_setup["dung"]
    mintesnot = shared_room_setup["mintesnot"]
    set_user = shared_room_setup["set_user"]
    session = shared_room_setup["session"]

    set_user(dung)
    res = client.post(
        "/api/v1/meetings/sync",
        json={"meeting_url": "https://meet.google.com/ai-test-room"},
    )
    meeting_id = res.json()["id"]

    # Mintesnot (PARTICIPANT) joins while meeting is in progress
    set_user(mintesnot)
    res_join = client.post(
        "/api/v1/meetings/sync",
        json={"meeting_url": "https://meet.google.com/ai-test-room"},
    )
    assert res_join.json()["id"] == meeting_id

    # Mock meeting completed with existing ai_result
    m = session.get(Meeting, uuid.UUID(meeting_id))
    m.status = "completed"
    m.ai_status = "COMPLETED"
    m.ai_result = {"summary": "Existing summary"}
    session.commit()

    # Participant calling force_reprocess when ai_result exists -> Must return 403 Forbidden
    res_ai_forbidden = client.post(
        f"/api/v1/meetings/{meeting_id}/ai/process",
        json={"force_reprocess": True},
    )
    assert res_ai_forbidden.status_code == 403
    assert "Chỉ chủ phòng" in res_ai_forbidden.json()["detail"]


def test_meeting_lifecycle_reaper(shared_room_setup):
    session = shared_room_setup["session"]
    dung = shared_room_setup["dung"]
    now = datetime.now(timezone.utc)

    # Create meeting and active live session
    m_repo = MeetingRepository(session)
    m = m_repo.create(
        {
            "user_id": dung.id,
            "title": "Orphaned Meeting",
            "status": "in_progress",
            "created_at": now - timedelta(minutes=10),
        }
    )

    ls = LiveSession(
        session_id=uuid.uuid4(),
        meeting_id=m.id,
        user_id=dung.id,
        tab_session_uuid=uuid.uuid4(),
        status="ACTIVE",
        last_heartbeat_at=now - timedelta(minutes=5),  # timed out > 90s
    )
    session.add(ls)
    session.commit()

    reaper = MeetingLifecycleService(session)

    # 1. Reap timed out sessions
    reaped_sessions = reaper.reap_timed_out_sessions(ttl_seconds=90)
    assert reaped_sessions == 1
    session.refresh(ls)
    assert ls.status == "TIMED_OUT"

    # 2. Reap orphaned meeting (no active sessions left, created > 120s ago)
    reaped_meetings = reaper.reap_orphaned_meetings(session_ttl_seconds=90, min_age_seconds=120)
    assert reaped_meetings == 1
    session.refresh(m)
    assert m.status == "completed"
