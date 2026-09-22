import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models import Meeting, Participant, Transcript, TranscriptEntry, User
from app.repositories import MeetingRepository, UserRepository


@pytest.fixture()
def auth_setup(db_session: Session):
    user_repo = UserRepository(db_session)
    user_a = user_repo.create(
        {
            "google_user_id": "google-user-tx-111",
            "email": "user_tx_a@example.com",
            "display_name": "User Tx Alpha",
        }
    )
    user_b = user_repo.create(
        {
            "google_user_id": "google-user-tx-222",
            "email": "user_tx_b@example.com",
            "display_name": "User Tx Beta",
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


def test_transcript_view_official_preferred(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    session = auth_setup["session"]

    # Create meeting with BOTH Official and DOM
    m = MeetingRepository(session).create(
        {
            "user_id": user_a.id,
            "title": "Hybrid Meeting",
            "dom_capture_status": "captured",
            "dom_transcript_data": [
                {"speaker": "DOM Speaker", "text": "This is DOM text", "start_time": "00:00:05"}
            ],
        }
    )

    part = Participant(
        meeting_id=m.id,
        google_participant_name="spaces/space1/participants/p1",
        display_name="Official Speaker",
        email="official@example.com",
    )
    session.add(part)
    session.flush()

    tx = Transcript(
        meeting_id=m.id,
        google_transcript_name="transcripts/t1",
        state="FILE_GENERATED",
    )
    session.add(tx)
    session.flush()

    entry = TranscriptEntry(
        transcript_id=tx.id,
        participant_id=part.id,
        google_entry_name="entries/e1",
        text="This is official transcript line.",
        start_time=datetime(2026, 9, 15, 10, 1, 30, tzinfo=timezone.utc),
    )
    session.add(entry)
    session.commit()

    # Query transcript view
    res = client.get(f"/api/v1/meetings/{m.id}/transcript-view")
    assert res.status_code == 200
    data = res.json()
    assert data["source"] == "OFFICIAL"
    assert data["total_entries"] == 1
    assert data["entries"][0]["speaker"] == "Official Speaker"
    assert data["entries"][0]["text"] == "This is official transcript line."
    assert "10:01:30" in data["entries"][0]["timestamp"]

    # Invariant: NEVER merge DOM text into official view
    all_texts = [e["text"] for e in data["entries"]]
    assert "This is DOM text" not in all_texts


def test_transcript_view_dom_fallback(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    session = auth_setup["session"]

    # Meeting with ONLY DOM captions
    m = MeetingRepository(session).create(
        {
            "user_id": user_a.id,
            "title": "DOM Only Meeting",
            "dom_capture_status": "captured",
            "dom_transcript_data": {
                "segments": [
                    {
                        "speaker": "Tran Thi B",
                        "text": "Phu thuoc vao DOM fallback.",
                        "start_time": "00:02:15",
                    },
                    {
                        "speaker": "Nguyen Van A",
                        "text": "Nhat tri voi phuong an nay.",
                        "start_time": "00:02:40",
                    },
                ]
            },
        }
    )

    res = client.get(f"/api/v1/meetings/{m.id}/transcript-view")
    assert res.status_code == 200
    data = res.json()
    assert data["source"] == "DOM"
    assert data["total_entries"] == 2
    assert data["entries"][0]["speaker"] == "Tran Thi B"
    assert data["entries"][0]["text"] == "Phu thuoc vao DOM fallback."
    assert data["entries"][0]["timestamp"] == "00:02:15"
    assert data["entries"][1]["speaker"] == "Nguyen Van A"


def test_transcript_view_none_when_empty(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    session = auth_setup["session"]

    m = MeetingRepository(session).create(
        {
            "user_id": user_a.id,
            "title": "Empty Meeting",
        }
    )

    res = client.get(f"/api/v1/meetings/{m.id}/transcript-view")
    assert res.status_code == 200
    data = res.json()
    assert data["source"] == "NONE"
    assert data["total_entries"] == 0
    assert data["entries"] == []


def test_transcript_view_user_isolation(auth_setup):
    client = auth_setup["client"]
    user_b = auth_setup["user_b"]
    session = auth_setup["session"]

    m_b = MeetingRepository(session).create(
        {
            "user_id": user_b.id,
            "title": "User B Meeting",
            "dom_transcript_data": [{"speaker": "Bob", "text": "Secret", "start_time": "00:01:00"}],
        }
    )

    # Current user is User A -> gets 404
    res = client.get(f"/api/v1/meetings/{m_b.id}/transcript-view")
    assert res.status_code == 404
