import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy.orm import Session

from app.models import Meeting, MeetingRole, User
from app.repositories import MeetingAccessRepository, MeetingRepository
from app.services.conference_identity_resolver import ConferenceIdentityResolver


def test_zoom_code_extraction_and_url_sanitization(db_session: Session):
    resolver = ConferenceIdentityResolver(db_session)

    # 1. URL extraction
    url1 = "https://app.zoom.us/wc/1234567890/join?pwd=SECRET_PASSWORD_123"
    sanitized1 = resolver.sanitize_meeting_url(url1, platform="ZOOM_WEB")
    assert "pwd=" not in sanitized1
    assert "SECRET_PASSWORD_123" not in sanitized1
    assert resolver.extract_meeting_code(sanitized1, None, platform="ZOOM_WEB") == "1234567890"

    # 2. Path extraction /j/
    url2 = "https://us04web.zoom.us/j/98765432109?pwd=SECRET"
    sanitized2 = resolver.sanitize_meeting_url(url2, platform="ZOOM_WEB")
    assert "pwd=" not in sanitized2
    assert resolver.extract_meeting_code(sanitized2, None, platform="ZOOM_WEB") == "98765432109"

    # 3. Meeting title normalization
    assert resolver.normalize_meeting_title(None, "1234567890", platform="ZOOM_WEB") == "Zoom - 1234567890"
    assert resolver.normalize_meeting_title("Zoom Meeting", "1234567890", platform="ZOOM_WEB") == "Zoom - 1234567890"
    assert resolver.normalize_meeting_title("Zoom Meeting (1234567890)", "1234567890", platform="ZOOM_WEB") == "Zoom - 1234567890"
    assert resolver.normalize_meeting_title("Sprint Planning Daily", "1234567890", platform="ZOOM_WEB") == "Sprint Planning Daily"


def test_zoom_shared_room_creation_and_multiuser_join(db_session: Session):
    uid_a = uuid.uuid4().hex[:6]
    uid_b = uuid.uuid4().hex[:6]
    user_a = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid_a}",
        email=f"zooma_{uid_a}@example.com",
        display_name="Zoom User A",
    )
    user_b = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid_b}",
        email=f"zoomb_{uid_b}@example.com",
        display_name="Zoom User B",
    )
    db_session.add_all([user_a, user_b])
    db_session.commit()

    resolver = ConferenceIdentityResolver(db_session)
    access_repo = MeetingAccessRepository(db_session)

    zoom_url = "https://app.zoom.us/wc/8899001122/join?pwd=TOP_SECRET_HASH"

    # User A starts the Zoom meeting
    m1, is_created_1 = resolver.resolve_or_create_shared_meeting(
        user_id=user_a.id,
        meeting_url=zoom_url,
        title="",
        platform="ZOOM_WEB",
    )

    assert is_created_1 is True
    assert m1.platform == "ZOOM_WEB"
    assert m1.conference_identity == "zoom_8899001122"
    assert m1.meeting_code == "8899001122"
    assert m1.title == "Zoom - 8899001122"
    assert "pwd=" not in m1.meeting_url
    assert access_repo.get_user_role(m1.id, user_a.id) == MeetingRole.OWNER.value

    # User B joins the same Zoom meeting
    m2, is_created_2 = resolver.resolve_or_create_shared_meeting(
        user_id=user_b.id,
        meeting_url=zoom_url,
        title="Zoom",
        platform="ZOOM_WEB",
    )

    assert is_created_2 is False
    assert m2.id == m1.id
    assert m2.platform == "ZOOM_WEB"
    assert access_repo.get_user_role(m1.id, user_b.id) == MeetingRole.PARTICIPANT.value


def test_platform_isolation_google_meet_vs_zoom(db_session: Session):
    uid = uuid.uuid4().hex[:6]
    user = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid}",
        email=f"iso_{uid}@example.com",
        display_name="Isolation User",
    )
    db_session.add(user)
    db_session.commit()

    resolver = ConferenceIdentityResolver(db_session)

    # Google Meet with code "abc-defg-hij"
    meet_m, is_created_meet = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url="https://meet.google.com/abc-defg-hij",
        title="Google Meet Session",
        platform="GOOGLE_MEET",
    )
    assert is_created_meet is True
    assert meet_m.platform == "GOOGLE_MEET"
    assert meet_m.title == "Meet - abc-defg-hij"
    assert meet_m.meeting_code == "abc-defg-hij"

    # Zoom with numeric code "1234567890"
    zoom_m, is_created_zoom = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url="https://app.zoom.us/wc/1234567890/join",
        title="Zoom",
        platform="ZOOM_WEB",
    )
    assert is_created_zoom is True
    assert zoom_m.platform == "ZOOM_WEB"
    assert zoom_m.title == "Zoom - 1234567890"
    assert zoom_m.meeting_code == "1234567890"
    assert zoom_m.id != meet_m.id


def test_zoom_orphan_cleanup_creates_new_meeting(db_session: Session):
    from datetime import datetime, timedelta, timezone

    uid = uuid.uuid4().hex[:6]
    user = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid}",
        email=f"orphan_{uid}@example.com",
        display_name="Orphan Test User",
    )
    db_session.add(user)
    db_session.commit()

    resolver = ConferenceIdentityResolver(db_session)
    zoom_url = "https://app.zoom.us/wc/81356877750/join"

    # Step 1: User starts an earlier meeting
    m1, is_created_1 = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url=zoom_url,
        title="Nguyễn Đức Dũng's Zoom Meeting",
        platform="ZOOM_WEB",
    )
    assert is_created_1 is True
    assert m1.status == "in_progress"

    # Simulate that m1 was created 15 minutes ago and has no active heartbeats in the last 120s
    past_time = datetime.now(timezone.utc) - timedelta(minutes=15)
    m1.created_at = past_time
    m1.last_heartbeat_at = past_time
    db_session.commit()

    # Step 2: User starts a new session on the same Zoom URL
    m2, is_created_2 = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url=zoom_url,
        title="Nguyễn Đức Dũng's Zoom Meeting",
        platform="ZOOM_WEB",
    )

    # Verification: Old orphan meeting m1 was closed, new meeting m2 was created!
    assert is_created_2 is True
    assert m2.id != m1.id
    assert m2.status == "in_progress"

    # Check that m1 was cleanly transitioned to completed
    db_session.refresh(m1)
    assert m1.status == "completed"
    assert m1.end_time is not None


def test_zoom_explicit_meeting_code_param(db_session: Session):
    uid = uuid.uuid4().hex[:6]
    user = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid}",
        email=f"explicit_{uid}@example.com",
        display_name="Explicit Code User",
    )
    db_session.add(user)
    db_session.commit()

    resolver = ConferenceIdentityResolver(db_session)
    # meeting_url is raw /join without numeric code, but meeting_code is passed explicitly
    m, is_created = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url="https://app.zoom.us/wc/join",
        title="Zoom",
        platform="ZOOM_WEB",
        meeting_code="86492899103",
    )
    assert is_created is True
    assert m.meeting_code == "86492899103"
    assert m.conference_identity == "zoom_86492899103"
    assert m.title == "Zoom - 86492899103"

