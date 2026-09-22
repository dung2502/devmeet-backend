import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models import LiveSession, LiveSessionStatus, Meeting, MeetingAccess, MeetingDOMSegment, MeetingRole, User
from app.repositories import MeetingAccessRepository, MeetingDOMSegmentRepository, MeetingRepository


def create_user(db: Session, email: str, name: str) -> User:
    user = User(
        google_user_id=f"gid-{email}",
        email=email,
        display_name=name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_m3_multi_client_shared_room_resolution(db_session: Session) -> None:
    """Verify that multiple clients joining same meeting_code resolve to single meeting_id with RBAC."""
    user_a = create_user(db_session, "alice@example.com", "Alice")
    user_b = create_user(db_session, "bob@example.com", "Bob")

    meeting_code = "shared-sync-room-101"
    space_name = f"spaces/{meeting_code}"

    # Override get_db fixture
    app.dependency_overrides[get_db] = lambda: db_session

    # 1. User A syncs room
    app.dependency_overrides[get_current_user] = lambda: user_a
    client = TestClient(app)

    res_a = client.post(
        "/api/v1/meetings/sync",
        json={
            "meeting_space_name": space_name,
            "meeting_url": f"https://meet.google.com/{meeting_code}",
            "title": "Team Sync Call",
        },
    )
    assert res_a.status_code == 200
    data_a = res_a.json()
    meeting_id = data_a["id"]

    # Verify User A is OWNER in meeting_access
    access_repo = MeetingAccessRepository(db_session)
    access_a = access_repo.get_access(uuid.UUID(meeting_id), user_a.id)
    assert access_a is not None
    assert access_a.role == MeetingRole.OWNER.value

    # 2. User B syncs SAME room
    app.dependency_overrides[get_current_user] = lambda: user_b
    res_b = client.post(
        "/api/v1/meetings/sync",
        json={
            "meeting_space_name": space_name,
            "meeting_url": f"https://meet.google.com/{meeting_code}",
            "title": "Team Sync Call",
        },
    )
    assert res_b.status_code == 200
    data_b = res_b.json()

    # Must resolve to EXACT SAME meeting_id
    assert data_b["id"] == meeting_id

    # Verify User B is PARTICIPANT in meeting_access
    access_b = access_repo.get_access(uuid.UUID(meeting_id), user_b.id)
    assert access_b is not None
    assert access_b.role == MeetingRole.PARTICIPANT.value

    # Clean up overrides
    app.dependency_overrides.clear()


def test_m3_heartbeat_and_dual_write_finalization(db_session: Session) -> None:
    """Verify heartbeat loop, clock calibration, and dual-write to raw event store."""
    user = create_user(db_session, "charlie@example.com", "Charlie")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)

    # 1. Create meeting and live session
    res_sync = client.post(
        "/api/v1/meetings/sync",
        json={"meeting_space_name": "spaces/charlie-room-1"},
    )
    meeting_id = res_sync.json()["id"]

    tab_uuid = str(uuid.uuid4())
    res_sess = client.post(
        "/api/v1/live-sessions",
        json={"meeting_id": meeting_id, "tab_session_uuid": tab_uuid},
    )
    assert res_sess.status_code == 201
    session_id = res_sess.json()["session_id"]

    # 2. Send Heartbeat
    client_now_ms = 1700000000000
    res_hb = client.post(
        f"/api/v1/live-sessions/{session_id}/heartbeat",
        json={"client_timestamp_ms": client_now_ms},
    )
    assert res_hb.status_code == 200
    hb_data = res_hb.json()
    assert hb_data["session_id"] == session_id
    assert hb_data["active_capture_sessions"] == 1
    assert "clock_offset_ms" in hb_data

    # 3. Finalize with Dual-Write
    finalize_payload = {
        "meeting_id": meeting_id,
        "captured_at": "2026-09-15T11:00:00Z",
        "segment_count": 2,
        "segments": [
            {
                "segment_id": str(uuid.uuid4()),
                "sequence": 1,
                "speaker_name": "Charlie",
                "text": "Xin chào, tôi là Charlie.",
                "observed_start_epoch_ms": 1700000001000,
                "observed_end_epoch_ms": 1700000003000,
            },
            {
                "segment_id": str(uuid.uuid4()),
                "sequence": 2,
                "speaker_name": "Charlie",
                "text": "Chúng ta bắt đầu nhé.",
                "observed_start_epoch_ms": 1700000003500,
                "observed_end_epoch_ms": 1700000005000,
            },
        ],
    }
    res_fin = client.post(
        f"/api/v1/live-sessions/{session_id}/finalize",
        json=finalize_payload,
    )
    assert res_fin.status_code == 201
    fin_data = res_fin.json()
    assert fin_data["raw_segments_ingested"] == 2

    # Verify Dual-Write: Raw table has 2 rows
    dom_repo = MeetingDOMSegmentRepository(db_session)
    raw_segs = dom_repo.list_by_meeting_id(uuid.UUID(meeting_id))
    assert len(raw_segs) == 2
    assert raw_segs[0].text == "Xin chào, tôi là Charlie."

    # Verify Dual-Write: Legacy meetings.dom_transcript_data populated
    meeting = db_session.get(Meeting, uuid.UUID(meeting_id))
    assert meeting is not None
    assert meeting.dom_transcript_data is not None
    assert "segments" in meeting.dom_transcript_data

    app.dependency_overrides.clear()


def test_m3_debounce_grace_period_state_machine(db_session: Session) -> None:
    """Verify that when 1 client leaves while another is active, meeting enters 180s grace period."""
    user_a = create_user(db_session, "host@example.com", "Host")
    user_b = create_user(db_session, "guest@example.com", "Guest")

    meeting_repo = MeetingRepository(db_session)
    meeting = meeting_repo.create({
        "user_id": user_a.id,
        "title": "Debounce Meeting",
        "conference_identity": "debounce-room-99",
        "status": "in_progress",
    })

    # Create active session for Guest (User B) with recent heartbeat
    sess_b = LiveSession(
        session_id=uuid.uuid4(),
        meeting_id=meeting.id,
        user_id=user_b.id,
        tab_session_uuid=uuid.uuid4(),
        status=LiveSessionStatus.ACTIVE.value,
        last_heartbeat_at=datetime.now(UTC),
    )
    db_session.add(sess_b)
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user_a
    client = TestClient(app)

    # Host (User A) calls ended
    res_ended = client.post(f"/api/v1/meetings/{meeting.id}/ended")
    assert res_ended.status_code == 200
    ended_data = res_ended.json()

    # Meeting must remain in_progress due to active Guest session
    assert ended_data["status"] == "in_progress"
    assert "grace period" in ended_data["message"]

    db_session.refresh(meeting)
    assert meeting.grace_period_expires_at is not None

    # Now simulate Guest (User B) disconnecting (last_heartbeat older than 90s)
    sess_b.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=100)
    db_session.commit()

    # Host calls ended again
    res_ended2 = client.post(f"/api/v1/meetings/{meeting.id}/ended")
    assert res_ended2.status_code == 200
    ended_data2 = res_ended2.json()

    # Meeting now completes
    assert ended_data2["status"] == "completed"

    app.dependency_overrides.clear()
