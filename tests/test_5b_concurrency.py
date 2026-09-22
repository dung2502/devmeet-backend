import threading
import uuid
import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from app.models import LiveSession, LiveSessionStatus, Meeting, User
from app.repositories import LiveSessionRepository, MeetingRepository, UserRepository


@pytest.fixture()
def test_env(db_session: Session, test_engine, test_schema_name: str) -> dict:
    session_factory = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    db = session_factory()
    user_repo = UserRepository(db)
    user = user_repo.create(
        {
            "google_user_id": f"google-user-conc-{uuid.uuid4().hex[:8]}",
            "email": f"conc_{uuid.uuid4().hex[:8]}@example.com",
            "display_name": "Concurrent User",
        }
    )
    meeting_repo = MeetingRepository(db)
    meeting = meeting_repo.create_extension_meeting(
        {
            "user_id": user.id,
            "conference_record_name": None,
            "meeting_url": "https://meet.google.com/concurrent-test",
            "title": "Concurrency Meeting",
        }
    )
    db.close()
    return {
        "engine": test_engine,
        "session_factory": session_factory,
        "user_id": user.id,
        "meeting_id": meeting.id,
        "schema_name": test_schema_name,
    }


def test_d1_concurrent_same_tab_converges_on_same_session(test_env: dict) -> None:
    session_factory = test_env["session_factory"]
    user_id = test_env["user_id"]
    meeting_id = test_env["meeting_id"]
    tab_uuid = uuid.uuid4()

    results = []
    errors = []

    def worker():
        db = session_factory()
        try:
            repo = LiveSessionRepository(db)
            session, is_created = repo.create_or_get_or_supersede_session(
                meeting_id=meeting_id,
                user_id=user_id,
                tab_session_uuid=tab_uuid,
            )
            results.append((session.session_id, is_created))
        except Exception as e:
            errors.append(e)
        finally:
            db.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0
    assert len(results) == 2
    assert results[0][0] == results[1][0]

    db = session_factory()
    count = db.query(LiveSession).filter(
        LiveSession.user_id == user_id,
        LiveSession.meeting_id == meeting_id,
        LiveSession.tab_session_uuid == tab_uuid,
    ).count()
    db.close()
    assert count == 1


def test_d2_concurrent_different_tabs_leaves_exactly_one_active_session(test_env: dict) -> None:
    session_factory = test_env["session_factory"]
    user_id = test_env["user_id"]
    meeting_id = test_env["meeting_id"]

    results = []
    errors = []

    def worker(tab_id):
        db = session_factory()
        try:
            repo = LiveSessionRepository(db)
            session, is_created = repo.create_or_get_or_supersede_session(
                meeting_id=meeting_id,
                user_id=user_id,
                tab_session_uuid=tab_id,
            )
            results.append((session.session_id, session.status, is_created))
        except Exception as e:
            errors.append(e)
        finally:
            db.close()

    t1 = threading.Thread(target=worker, args=(uuid.uuid4(),))
    t2 = threading.Thread(target=worker, args=(uuid.uuid4(),))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0

    db = session_factory()
    active_sessions = db.query(LiveSession).filter(
        LiveSession.user_id == user_id,
        LiveSession.meeting_id == meeting_id,
        LiveSession.status == LiveSessionStatus.ACTIVE.value,
    ).all()
    all_sessions = db.query(LiveSession).filter(
        LiveSession.user_id == user_id,
        LiveSession.meeting_id == meeting_id,
    ).all()
    db.close()

    assert len(active_sessions) == 1
    assert len(all_sessions) == 2


def test_d3_db_partial_unique_index_exists(db_session: Session, test_engine) -> None:
    insp = inspect(db_session.bind)
    indexes = insp.get_indexes("live_sessions")
    partial_indexes = [
        idx for idx in indexes
        if idx.get("name") == "uq_live_sessions_active_user_meeting"
    ]
    assert len(partial_indexes) == 1
    assert partial_indexes[0]["unique"] is True


def test_d4_db_composite_unique_creation_identity_exists(db_session: Session, test_engine) -> None:
    insp = inspect(db_session.bind)
    unique_constraints = insp.get_unique_constraints("live_sessions")
    idempotency_uq = [
        uq for uq in unique_constraints
        if uq.get("name") == "uq_live_sessions_user_meeting_tab"
    ]
    assert len(idempotency_uq) == 1
    assert set(idempotency_uq[0]["column_names"]) == {"user_id", "meeting_id", "tab_session_uuid"}


def test_d5_transaction_rollback_preserves_clean_state(test_env: dict) -> None:
    session_factory = test_env["session_factory"]
    user_id = test_env["user_id"]
    non_existent_meeting = uuid.uuid4()

    db = session_factory()
    repo = LiveSessionRepository(db)
    with pytest.raises(Exception):
        repo.create_or_get_or_supersede_session(
            meeting_id=non_existent_meeting,
            user_id=user_id,
            tab_session_uuid=uuid.uuid4(),
        )
    db.close()

    db = session_factory()
    count = db.query(LiveSession).filter(LiveSession.meeting_id == non_existent_meeting).count()
    db.close()
    assert count == 0
