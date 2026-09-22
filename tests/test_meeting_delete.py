import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models import (
    LiveSession,
    Meeting,
    MeetingAccess,
    MeetingDOMSegment,
    Participant,
    Transcript,
    TranscriptEntry,
    User,
)
from app.repositories import UserRepository


@pytest.fixture()
def delete_test_setup(db_session: Session):
    user_repo = UserRepository(db_session)
    user_owner = user_repo.create(
        {
            "google_user_id": "google-owner-123",
            "email": "owner@example.com",
            "display_name": "Meeting Owner",
        }
    )
    user_other = user_repo.create(
        {
            "google_user_id": "google-other-456",
            "email": "other@example.com",
            "display_name": "Other User",
        }
    )

    current_active_user = [user_owner]

    def override_get_db():
        yield db_session

    def override_get_current_user():
        return current_active_user[0]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as client:
        yield {
            "client": client,
            "owner": user_owner,
            "other": user_other,
            "set_user": lambda u: current_active_user.__setitem__(0, u),
            "session": db_session,
        }

    app.dependency_overrides.clear()


def test_delete_meeting_success_and_cascade(delete_test_setup):
    client = delete_test_setup["client"]
    owner = delete_test_setup["owner"]
    session: Session = delete_test_setup["session"]

    # 1. Create meeting
    meeting = Meeting(
        user_id=owner.id,
        conference_record_name="conferenceRecords/test-del-1",
        meeting_space_name="spaces/del-test-1",
        title="Test Delete Meeting",
        status="completed",
        start_time=datetime.now(timezone.utc),
    )
    session.add(meeting)
    session.flush()

    meeting_id = meeting.id

    # 2. Add child entities
    participant = Participant(
        meeting_id=meeting_id,
        google_participant_name="participants/del-p-1",
        display_name="Participant 1",
    )
    session.add(participant)

    transcript = Transcript(
        meeting_id=meeting_id,
        google_transcript_name="transcripts/del-t-1",
        state="ACTIVE",
    )
    session.add(transcript)
    session.flush()

    entry = TranscriptEntry(
        transcript_id=transcript.id,
        google_entry_name="entries/del-e-1",
        participant_id=participant.id,
        text="Hello world test delete",
    )
    session.add(entry)

    live_session = LiveSession(
        meeting_id=meeting_id,
        user_id=owner.id,
        session_id=str(uuid.uuid4()),
        tab_session_uuid=uuid.uuid4(),
        status="ended",
    )
    session.add(live_session)

    dom_segment = MeetingDOMSegment(
        meeting_id=meeting_id,
        session_id=live_session.session_id,
        sequence=1,
        speaker_name="Speaker 1",
        text="DOM Segment text",
        observed_start_epoch_ms=1700000000000,
        observed_end_epoch_ms=1700000005000,
    )
    session.add(dom_segment)

    session.commit()

    # 3. Call DELETE /api/v1/meetings/{meeting_id} as owner
    resp = client.delete(f"/api/v1/meetings/{meeting_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert data["meeting_id"] == str(meeting_id)

    # 4. Verify Meeting is removed from DB
    assert session.get(Meeting, meeting_id) is None

    # 5. Verify all child entities cascaded
    assert session.scalar(select(Participant).where(Participant.meeting_id == meeting_id)) is None
    assert session.scalar(select(Transcript).where(Transcript.meeting_id == meeting_id)) is None
    assert session.scalar(select(TranscriptEntry).where(TranscriptEntry.google_entry_name == "entries/del-e-1")) is None
    assert session.scalar(select(LiveSession).where(LiveSession.meeting_id == meeting_id)) is None
    assert session.scalar(select(MeetingDOMSegment).where(MeetingDOMSegment.meeting_id == meeting_id)) is None


def test_delete_meeting_forbidden_for_non_owner(delete_test_setup):
    client = delete_test_setup["client"]
    owner = delete_test_setup["owner"]
    other = delete_test_setup["other"]
    session: Session = delete_test_setup["session"]

    meeting = Meeting(
        user_id=owner.id,
        conference_record_name="conferenceRecords/test-del-2",
        title="Owner Meeting",
    )
    session.add(meeting)
    session.commit()
    session.refresh(meeting)

    # Switch client to 'other' user
    delete_test_setup["set_user"](other)

    resp = client.delete(f"/api/v1/meetings/{meeting.id}")
    assert resp.status_code == 403

    # Meeting still exists
    assert session.get(Meeting, meeting.id) is not None


def test_delete_meeting_not_found(delete_test_setup):
    client = delete_test_setup["client"]
    random_id = uuid.uuid4()

    resp = client.delete(f"/api/v1/meetings/{random_id}")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"]["code"] == "MEETING_NOT_FOUND"


def test_delete_meeting_by_granted_host(delete_test_setup):
    client = delete_test_setup["client"]
    owner = delete_test_setup["owner"]
    other = delete_test_setup["other"]
    session: Session = delete_test_setup["session"]

    meeting = Meeting(
        user_id=owner.id,
        conference_record_name="conferenceRecords/test-del-host",
        title="Owner Meeting With Host Access",
        status="completed",
    )
    session.add(meeting)
    session.flush()

    access = MeetingAccess(
        meeting_id=meeting.id,
        user_id=other.id,
        role="host",
    )
    session.add(access)
    session.commit()

    # Switch to 'other' who is host
    delete_test_setup["set_user"](other)

    resp = client.delete(f"/api/v1/meetings/{meeting.id}")
    assert resp.status_code == 200
    assert session.get(Meeting, meeting.id) is None
