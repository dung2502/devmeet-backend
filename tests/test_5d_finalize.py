import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models.meeting import Meeting
from app.models.live_session import LiveSession
from app.models.user import User
from app.utils.hashing import compute_transcript_content_hash


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _make_meeting(db: Session, user: User) -> Meeting:
    m = Meeting(
        id=uuid.uuid4(),
        user_id=user.id,
        meeting_url='https://meet.google.com/test-abc-def',
        title='Test Meeting',
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _make_session(db: Session, user: User, meeting: Meeting) -> LiveSession:
    s = LiveSession(
        session_id=uuid.uuid4(),
        meeting_id=meeting.id,
        user_id=user.id,
        tab_session_uuid=uuid.uuid4(),
        status='ACTIVE',
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


SAMPLE_SEGMENTS = [
    {'segment_id': 'seg-1', 'speaker_name': 'Alice', 'text': 'Hello world', 'sequence': 1, 'source': 'DOM'},
    {'segment_id': 'seg-2', 'speaker_name': 'Bob', 'text': 'Hi there', 'sequence': 2, 'source': 'DOM'},
]

SAMPLE_PAYLOAD = {
    'meeting_id': None,  # filled per test
    'segments': SAMPLE_SEGMENTS,
    'segment_count': 2,
    'captured_at': '2026-09-10T09:00:00Z',
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_c1_finalize_valid_payload_returns_201(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    ls = _make_session(db_session, user, meeting)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    payload = {**SAMPLE_PAYLOAD, 'meeting_id': str(meeting.id)}
    res = client.post(f'/api/v1/live-sessions/{ls.session_id}/finalize', json=payload)
    app.dependency_overrides.clear()
    assert res.status_code == 201, res.text
    data = res.json()
    assert data['session_id'] == str(ls.session_id)
    assert data['meeting_id'] == str(meeting.id)
    assert data['dom_capture_status'] == 'received'
    assert data['status'] == 'accepted'


def test_c2_finalize_same_payload_retry_returns_200(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    ls = _make_session(db_session, user, meeting)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    payload = {**SAMPLE_PAYLOAD, 'meeting_id': str(meeting.id)}
    res1 = client.post(f'/api/v1/live-sessions/{ls.session_id}/finalize', json=payload)
    assert res1.status_code == 201
    res2 = client.post(f'/api/v1/live-sessions/{ls.session_id}/finalize', json=payload)
    app.dependency_overrides.clear()
    assert res2.status_code == 200, res2.text
    assert res2.json()['status'] == 'accepted'


def test_c3_finalize_different_payload_same_session_returns_409(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    ls = _make_session(db_session, user, meeting)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    payload1 = {**SAMPLE_PAYLOAD, 'meeting_id': str(meeting.id)}
    res1 = client.post(f'/api/v1/live-sessions/{ls.session_id}/finalize', json=payload1)
    assert res1.status_code == 201
    payload2 = {
        'meeting_id': str(meeting.id),
        'segments': [{'segment_id': 'seg-x', 'speaker_name': 'Charlie', 'text': 'Different text', 'sequence': 1, 'source': 'DOM'}],
        'segment_count': 1,
        'captured_at': '2026-09-10T10:00:00Z',
    }
    res2 = client.post(f'/api/v1/live-sessions/{ls.session_id}/finalize', json=payload2)
    app.dependency_overrides.clear()
    assert res2.status_code == 409, res2.text
    assert res2.json()['error']['code'] == 'SESSION_PAYLOAD_CONFLICT'


def test_c4_finalize_unknown_session_returns_404(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    fake_session_id = uuid.uuid4()
    payload = {**SAMPLE_PAYLOAD, 'meeting_id': str(meeting.id)}
    res = client.post(f'/api/v1/live-sessions/{fake_session_id}/finalize', json=payload)
    app.dependency_overrides.clear()
    assert res.status_code == 404, res.text
    assert res.json()['error']['code'] == 'LIVE_SESSION_NOT_FOUND'


def test_c5_finalize_wrong_user_returns_404(db_session: Session) -> None:
    owner = _make_user(db_session)
    attacker = _make_user(db_session)
    meeting = _make_meeting(db_session, owner)
    ls = _make_session(db_session, owner, meeting)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: attacker
    app.dependency_overrides[get_db] = lambda: db_session
    payload = {**SAMPLE_PAYLOAD, 'meeting_id': str(meeting.id)}
    res = client.post(f'/api/v1/live-sessions/{ls.session_id}/finalize', json=payload)
    app.dependency_overrides.clear()
    assert res.status_code == 404, res.text


def test_c6_finalize_persists_dom_transcript_data(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    ls = _make_session(db_session, user, meeting)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    payload = {**SAMPLE_PAYLOAD, 'meeting_id': str(meeting.id)}
    res = client.post(f'/api/v1/live-sessions/{ls.session_id}/finalize', json=payload)
    app.dependency_overrides.clear()
    assert res.status_code == 201
    db_session.expire_all()
    updated_meeting = db_session.get(Meeting, meeting.id)
    assert updated_meeting is not None
    assert updated_meeting.dom_transcript_data is not None
    assert updated_meeting.dom_transcript_data['segment_count'] == 2
    assert updated_meeting.dom_transcript_data['session_id'] == str(ls.session_id)
    assert 'content_hash' in updated_meeting.dom_transcript_data


def test_c7_finalize_sets_dom_capture_status_received(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    ls = _make_session(db_session, user, meeting)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    payload = {**SAMPLE_PAYLOAD, 'meeting_id': str(meeting.id)}
    client.post(f'/api/v1/live-sessions/{ls.session_id}/finalize', json=payload)
    app.dependency_overrides.clear()
    db_session.expire_all()
    updated_meeting = db_session.get(Meeting, meeting.id)
    assert updated_meeting.dom_capture_status == 'received'


def test_c8_finalize_does_not_alter_ai_status(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    original_ai_status = meeting.ai_status
    ls = _make_session(db_session, user, meeting)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    payload = {**SAMPLE_PAYLOAD, 'meeting_id': str(meeting.id)}
    client.post(f'/api/v1/live-sessions/{ls.session_id}/finalize', json=payload)
    app.dependency_overrides.clear()
    db_session.expire_all()
    updated_meeting = db_session.get(Meeting, meeting.id)
    assert updated_meeting.ai_status == original_ai_status


def test_c9_finalize_empty_segments_accepted(db_session: Session) -> None:
    user = _make_user(db_session)
    meeting = _make_meeting(db_session, user)
    ls = _make_session(db_session, user, meeting)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    payload = {'meeting_id': str(meeting.id), 'segments': [], 'segment_count': 0, 'captured_at': '2026-09-10T09:00:00Z'}
    res = client.post(f'/api/v1/live-sessions/{ls.session_id}/finalize', json=payload)
    app.dependency_overrides.clear()
    assert res.status_code == 201, res.text


def test_c10_hash_is_deterministic() -> None:
    segs = [
        {'segment_id': 'x', 'speaker_name': 'Alice', 'text': 'Hello', 'sequence': 1, 'source': 'DOM', 'captured_at': 111},
        {'segment_id': 'y', 'speaker_name': 'Bob', 'text': 'Hi', 'sequence': 2, 'source': 'DOM', 'captured_at': 222},
    ]
    h1 = compute_transcript_content_hash(segs)
    h2 = compute_transcript_content_hash(segs)
    assert h1 == h2
    assert h1.startswith('sha256:')


def test_c11_hash_is_content_based_excludes_ids_and_timestamps() -> None:
    segs_a = [{'segment_id': 'uuid-1', 'speaker_name': 'Alice', 'text': 'Hello', 'sequence': 1, 'source': 'DOM', 'captured_at': 100}]
    segs_b = [{'segment_id': 'uuid-DIFFERENT', 'speaker_name': 'Alice', 'text': 'Hello', 'sequence': 1, 'source': 'DOM', 'captured_at': 999}]
    assert compute_transcript_content_hash(segs_a) == compute_transcript_content_hash(segs_b)


def test_c12_hash_differs_on_different_content() -> None:
    segs_a = [{'segment_id': 'seg-1', 'speaker_name': 'Alice', 'text': 'Hello', 'sequence': 1, 'source': 'DOM'}]
    segs_b = [{'segment_id': 'seg-1', 'speaker_name': 'Alice', 'text': 'Goodbye', 'sequence': 1, 'source': 'DOM'}]
    assert compute_transcript_content_hash(segs_a) != compute_transcript_content_hash(segs_b)


def test_c13_finalize_with_dev_auth_bypass(db_session: Session) -> None:
    from app.config import get_settings
    settings = get_settings()
    if not settings.allow_dev_auth_bypass:
        pytest.skip('ALLOW_DEV_AUTH_BYPASS not enabled')
    # With bypass, a dev user is auto-created - test endpoint reachable without real token
    client = TestClient(app)
    app.dependency_overrides[get_db] = lambda: db_session
    # Reset overrides so auth bypass is exercised
    res = client.post('/api/v1/live-sessions/' + str(uuid.uuid4()) + '/finalize',
                      json={'meeting_id': str(uuid.uuid4()), 'segments': [], 'segment_count': 0, 'captured_at': '2026-09-10T09:00:00Z'})
    app.dependency_overrides.clear()
    # Expect 404 (session not found for dev user) not 401 (not unauthorized)
    assert res.status_code == 404, res.text


def test_c14_session_level_idempotency_multisession(db_session: Session) -> None:
    """
    Verify true SESSION-LEVEL idempotency:
    - User A Session A with Payload A -> 201
    - User B Session B (same meeting, different session) with Payload B -> 201 (NOT 409!)
    - Session A retry with Payload A -> 200 (idempotent)
    - Session A retry with Payload A' (conflicting) -> 409
    - Session B retry with Payload B -> 200 (idempotent)
    - Session B retry with Payload B' (conflicting) -> 409
    """
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    meeting = _make_meeting(db_session, user_a)
    session_a = _make_session(db_session, user_a, meeting)
    session_b = _make_session(db_session, user_b, meeting)

    client = TestClient(app)

    payload_a = {
        'meeting_id': str(meeting.id),
        'segments': [{'segment_id': 'seg-a1', 'speaker_name': 'Alice', 'text': 'Hello from Session A', 'sequence': 1, 'source': 'DOM'}],
        'segment_count': 1,
        'captured_at': '2026-09-10T09:00:00Z',
    }

    payload_b = {
        'meeting_id': str(meeting.id),
        'segments': [{'segment_id': 'seg-b1', 'speaker_name': 'Bob', 'text': 'Hello from Session B', 'sequence': 1, 'source': 'DOM'}],
        'segment_count': 1,
        'captured_at': '2026-09-10T09:05:00Z',
    }

    # 1. Session A first finalize (as user A) -> 201
    app.dependency_overrides[get_current_user] = lambda: user_a
    app.dependency_overrides[get_db] = lambda: db_session
    res_a1 = client.post(f'/api/v1/live-sessions/{session_a.session_id}/finalize', json=payload_a)
    assert res_a1.status_code == 201, res_a1.text

    # 2. Session B first finalize for SAME meeting (as user B) -> must succeed with 201 (not 409!)
    app.dependency_overrides[get_current_user] = lambda: user_b
    res_b1 = client.post(f'/api/v1/live-sessions/{session_b.session_id}/finalize', json=payload_b)
    assert res_b1.status_code == 201, res_b1.text

    # 3. Session A same payload retry (as user A) -> 200
    app.dependency_overrides[get_current_user] = lambda: user_a
    res_a_retry = client.post(f'/api/v1/live-sessions/{session_a.session_id}/finalize', json=payload_a)
    assert res_a_retry.status_code == 200, res_a_retry.text

    # 4. Session A different payload -> 409 conflict
    payload_a_conflict = {
        'meeting_id': str(meeting.id),
        'segments': [{'segment_id': 'seg-a1', 'speaker_name': 'Alice', 'text': 'Modified text for A', 'sequence': 1, 'source': 'DOM'}],
        'segment_count': 1,
        'captured_at': '2026-09-10T09:10:00Z',
    }
    res_a_conflict = client.post(f'/api/v1/live-sessions/{session_a.session_id}/finalize', json=payload_a_conflict)
    assert res_a_conflict.status_code == 409, res_a_conflict.text

    # 5. Session B same payload retry (as user B) -> 200
    app.dependency_overrides[get_current_user] = lambda: user_b
    res_b_retry = client.post(f'/api/v1/live-sessions/{session_b.session_id}/finalize', json=payload_b)
    assert res_b_retry.status_code == 200, res_b_retry.text

    # 6. Session B different payload -> 409 conflict
    payload_b_conflict = {
        'meeting_id': str(meeting.id),
        'segments': [{'segment_id': 'seg-b1', 'speaker_name': 'Bob', 'text': 'Modified text for B', 'sequence': 1, 'source': 'DOM'}],
        'segment_count': 1,
        'captured_at': '2026-09-10T09:15:00Z',
    }
    res_b_conflict = client.post(f'/api/v1/live-sessions/{session_b.session_id}/finalize', json=payload_b_conflict)
    assert res_b_conflict.status_code == 409, res_b_conflict.text

    app.dependency_overrides.clear()


