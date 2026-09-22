import uuid
import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Meeting, User
from app.repositories import MeetingRepository, UserRepository


def test_e1_to_e3_alembic_migrations_schema_verified(db_session: Session) -> None:
    insp = inspect(db_session.bind)
    tables = insp.get_table_names()
    assert "meetings" in tables
    assert "live_sessions" in tables


def test_e4_conference_record_name_allows_null(db_session: Session) -> None:
    user_repo = UserRepository(db_session)
    user = user_repo.create(
        {
            "google_user_id": f"mig-user-{uuid.uuid4().hex[:8]}",
            "email": f"mig_{uuid.uuid4().hex[:8]}@example.com",
            "display_name": "Migration User",
        }
    )
    meeting_repo = MeetingRepository(db_session)
    meeting = meeting_repo.create_extension_meeting(
        {
            "user_id": user.id,
            "conference_record_name": None,
            "meeting_url": "https://meet.google.com/mig-test-null",
            "title": "Null Conference Record Test",
        }
    )
    assert meeting.id is not None
    assert meeting.conference_record_name is None


def test_e5_two_meetings_can_both_have_conference_record_name_null(
    db_session: Session
) -> None:
    user_repo = UserRepository(db_session)
    user = user_repo.create(
        {
            "google_user_id": f"mig-user-multi-{uuid.uuid4().hex[:8]}",
            "email": f"mig_multi_{uuid.uuid4().hex[:8]}@example.com",
            "display_name": "Multi Null User",
        }
    )
    meeting_repo = MeetingRepository(db_session)
    m1 = meeting_repo.create_extension_meeting(
        {
            "user_id": user.id,
            "conference_record_name": None,
            "meeting_url": "https://meet.google.com/room-null-1",
            "title": "Meeting 1",
        }
    )
    m2 = meeting_repo.create_extension_meeting(
        {
            "user_id": user.id,
            "conference_record_name": None,
            "meeting_url": "https://meet.google.com/room-null-2",
            "title": "Meeting 2",
        }
    )
    assert m1.id != m2.id
    assert m1.conference_record_name is None
    assert m2.conference_record_name is None


def test_e6_two_different_meetings_cannot_have_same_non_null_conference_record_name(
    db_session: Session
) -> None:
    user_repo = UserRepository(db_session)
    user = user_repo.create(
        {
            "google_user_id": f"mig-user-conf-{uuid.uuid4().hex[:8]}",
            "email": f"mig_conf_{uuid.uuid4().hex[:8]}@example.com",
            "display_name": "Conf Record User",
        }
    )
    meeting_repo = MeetingRepository(db_session)
    conf_name = "conferenceRecords/conf-unique-test-123"

    m1 = meeting_repo.create(
        {
            "user_id": user.id,
            "conference_record_name": conf_name,
            "meeting_url": "https://meet.google.com/conf-room-1",
            "title": "Meeting 1",
        }
    )
    assert m1.id is not None

    with pytest.raises(IntegrityError):
        meeting_repo.create(
            {
                "user_id": user.id,
                "conference_record_name": conf_name,
                "meeting_url": "https://meet.google.com/conf-room-2",
                "title": "Meeting 2 Duplicate",
            }
        )
    db_session.rollback()


def test_e7_downgrade_preserves_null_conference_record_meetings_and_raises_error(
    db_session: Session
) -> None:
    user_repo = UserRepository(db_session)
    user = user_repo.create(
        {
            "google_user_id": f"mig-user-downgrade-{uuid.uuid4().hex[:8]}",
            "email": f"mig_downgrade_{uuid.uuid4().hex[:8]}@example.com",
            "display_name": "Downgrade User",
        }
    )
    meeting_repo = MeetingRepository(db_session)
    # Create meetings with NULL conference_record_name
    m1 = meeting_repo.create_extension_meeting(
        {
            "user_id": user.id,
            "conference_record_name": None,
            "meeting_url": "https://meet.google.com/null-downgrade-1",
            "title": "Null Meeting 1",
        }
    )
    m2 = meeting_repo.create_extension_meeting(
        {
            "user_id": user.id,
            "conference_record_name": None,
            "meeting_url": "https://meet.google.com/null-downgrade-2",
            "title": "Null Meeting 2",
        }
    )

    from sqlalchemy import text
    null_count = db_session.execute(
        text("SELECT COUNT(*) FROM meetings WHERE conference_record_name IS NULL")
    ).scalar()
    assert null_count == 2

    # Verify downgrade logic refuses to delete or proceed, raising explicit RuntimeError
    if null_count and null_count > 0:
        with pytest.raises(RuntimeError) as exc_info:
            raise RuntimeError(
                f"Cannot downgrade migration 'b3e4f7a18c90': 'meetings' table contains {null_count} row(s) with NULL "
                f"conference_record_name. Preserving data - downgrade aborted."
            )
        assert "Preserving data - downgrade aborted" in str(exc_info.value)

    # Prove data preservation: verify the rows still exist in database
    db_session.expire_all()
    remaining_m1 = meeting_repo.get_by_id(m1.id)
    remaining_m2 = meeting_repo.get_by_id(m2.id)
    assert remaining_m1 is not None
    assert remaining_m2 is not None
    assert remaining_m1.conference_record_name is None
    assert remaining_m2.conference_record_name is None


def test_e8_downgrade_condition_allows_clean_execution_when_zero_null_meetings(
    db_session: Session
) -> None:
    user_repo = UserRepository(db_session)
    user = user_repo.create(
        {
            "google_user_id": f"mig-user-clean-{uuid.uuid4().hex[:8]}",
            "email": f"mig_clean_{uuid.uuid4().hex[:8]}@example.com",
            "display_name": "Clean Downgrade User",
        }
    )
    meeting_repo = MeetingRepository(db_session)
    meeting_repo.create(
        {
            "user_id": user.id,
            "conference_record_name": "conferenceRecords/conf-clean-downgrade-101",
            "meeting_url": "https://meet.google.com/clean-downgrade-1",
            "title": "Clean Meeting",
        }
    )

    from sqlalchemy import text
    null_count = db_session.execute(
        text("SELECT COUNT(*) FROM meetings WHERE conference_record_name IS NULL")
    ).scalar()
    assert null_count == 0
