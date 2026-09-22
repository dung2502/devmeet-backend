import uuid
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models.meeting import Meeting
from app.models.user import User
from app.services.ended_service import EndedService


def _make_user(db: Session) -> User:
    u = User(
        id=uuid.uuid4(),
        google_user_id=f'gid-{uuid.uuid4().hex}',
        email=f'{uuid.uuid4().hex}@test.com',
        display_name='Test User',
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_meeting(db: Session, user: User, conference_record_name: str | None = None) -> Meeting:
    m = Meeting(
        id=uuid.uuid4(),
        user_id=user.id,
        meeting_url='https://meet.google.com/test-abc-def',
        title='Test Meeting',
        conference_record_name=conference_record_name,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _ended_url(meeting_id: uuid.UUID) -> str:
    return f'/api/v1/meetings/{meeting_id}/ended'


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_e1_ended_valid_returns_200_completed(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    res = client.post(_ended_url(meeting.id))
    app.dependency_overrides.clear()
    assert res.status_code == 200, res.text
    data = res.json()
    assert data['status'] == 'completed'
    assert data['transcript_status'] == 'processing'
    assert 'message' in data
    assert str(meeting.id) == data['id']


def test_e2_ended_repeated_is_idempotent(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    res1 = client.post(_ended_url(meeting.id))
    assert res1.status_code == 200
    res2 = client.post(_ended_url(meeting.id))
    app.dependency_overrides.clear()
    assert res2.status_code == 200, res2.text
    assert res2.json()['status'] == 'completed'
    # No crash, no duplicate side effects


def test_e3_ended_conference_record_name_none_no_crash(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user, conference_record_name=None)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    res = client.post(_ended_url(meeting.id))
    app.dependency_overrides.clear()
    assert res.status_code == 200, res.text
    assert res.json()['status'] == 'completed'


def test_e4_ended_unknown_meeting_returns_404(db_session: Session) -> None:
    user = _make_user(db_session)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    res = client.post(_ended_url(uuid.uuid4()))
    app.dependency_overrides.clear()
    assert res.status_code == 404, res.text


def test_e5_ended_wrong_user_returns_404(db_session: Session) -> None:
    owner = _make_user(db_session)
    attacker = _make_user(db_session)
    meeting = _make_meeting(db_session, owner)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: attacker
    app.dependency_overrides[get_db] = lambda: db_session
    res = client.post(_ended_url(meeting.id))
    app.dependency_overrides.clear()
    assert res.status_code == 404, res.text


def test_e6_ended_persists_status_in_db(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    client.post(_ended_url(meeting.id))
    app.dependency_overrides.clear()
    db_session.expire_all()
    updated = db_session.get(Meeting, meeting.id)
    assert updated.status == 'completed'
    assert updated.transcript_status == 'processing'


def test_e7_ended_already_completed_meeting_safe(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    meeting.status = 'completed'
    meeting.transcript_status = 'available'
    db_session.commit()
    db_session.refresh(meeting)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    res = client.post(_ended_url(meeting.id))
    app.dependency_overrides.clear()
    assert res.status_code == 200, res.text
    # transcript_status should NOT be reset by idempotent /ended
    db_session.expire_all()
    updated = db_session.get(Meeting, meeting.id)
    assert updated.transcript_status == 'available'


def test_e8_ended_without_prior_finalize_allowed(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    # No finalize called before ended - must still work
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    res = client.post(_ended_url(meeting.id))
    app.dependency_overrides.clear()
    assert res.status_code == 200, res.text
    assert res.json()['status'] == 'completed'


def test_e9_ended_does_not_affect_dom_capture_status(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    original_dom_status = meeting.dom_capture_status
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    client.post(_ended_url(meeting.id))
    app.dependency_overrides.clear()
    db_session.expire_all()
    updated = db_session.get(Meeting, meeting.id)
    # /ended must not alter dom_capture_status
    assert updated.dom_capture_status == original_dom_status


def test_e10_ended_with_conference_record_name_no_crash(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user, conference_record_name='conferenceRecords/test-123')
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    # Without token, deferred message is returned and status is completed
    res = client.post(_ended_url(meeting.id))
    app.dependency_overrides.clear()
    assert res.status_code == 200, res.text
    assert res.json()['status'] == 'completed'
    assert 'deferred' in res.json()['message']


def test_e11_ended_with_token_triggers_sync_scheduler(db_session: Session) -> None:
    """
    Verify that when conference_record_name AND Google Access Token are provided,
    the Official Transcript Sync scheduler/runner is actually invoked.
    """
    from unittest.mock import MagicMock
    from app.services.ended_service import EndedService

    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user, conference_record_name='conferenceRecords/conf-real-123')

    mock_runner = MagicMock()
    service = EndedService(session=db_session, sync_runner=mock_runner)

    # Call with google_access_token
    resp = service.mark_meeting_ended(
        meeting_id=meeting.id,
        user_id=user.id,
        google_access_token='ya29.sample_valid_token',
    )

    assert resp.status == 'completed'
    assert resp.transcript_status == 'processing'
    assert 'scheduled' in resp.message
    # Assert runner was actually invoked with correct parameters
    mock_runner.assert_called_once_with(
        meeting.id,
        'conferenceRecords/conf-real-123',
        user.id,
        'ya29.sample_valid_token',
    )


def test_e12_ended_endpoint_passes_auth_token_to_sync(db_session: Session) -> None:
    """
    Verify /ended endpoint extracts Bearer/X-Google-Access-Token and triggers sync via client.
    """
    from unittest.mock import patch

    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user, conference_record_name='conferenceRecords/conf-http-123')

    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session

    with patch.object(EndedService, '_default_sync_runner') as mock_default_runner:
        headers = {'Authorization': 'Bearer ya29.test_token_xyz'}
        res = client.post(_ended_url(meeting.id), headers=headers)
        app.dependency_overrides.clear()

        assert res.status_code == 200, res.text
        assert res.json()['status'] == 'completed'
        assert 'scheduled' in res.json()['message']
        mock_default_runner.assert_called_once()

