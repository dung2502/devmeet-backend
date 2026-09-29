import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote
import pytest
from sqlalchemy.orm import Session

from app.models import Meeting, MeetingRole, User
from app.repositories import MeetingAccessRepository, MeetingRepository
from app.services.conference_identity_resolver import ConferenceIdentityResolver


def test_teams_code_extraction_and_url_sanitization(db_session: Session):
    resolver = ConferenceIdentityResolver(db_session)

    # 1. URL extraction and sanitization with p, token, context
    url1 = "https://teams.microsoft.com/meet/241890123456?p=SECRET_P_TOKEN&token=AUTH_TOKEN&context=%7B%22tid%22%3A%22xyz%22%7D"
    sanitized1 = resolver.sanitize_meeting_url(url1, platform="MS_TEAMS")
    assert "p=" not in sanitized1
    assert "SECRET_P_TOKEN" not in sanitized1
    assert "token=" not in sanitized1
    assert "context=" not in sanitized1
    assert resolver.extract_meeting_code(sanitized1, None, platform="MS_TEAMS") == "241890123456"

    # 2. Path extraction /meet/ on teams.live.com
    url2 = "https://teams.live.com/meet/1234567890?p=TEST"
    sanitized2 = resolver.sanitize_meeting_url(url2, platform="MS_TEAMS")
    assert resolver.extract_meeting_code(sanitized2, None, platform="MS_TEAMS") == "1234567890"

    # 3. Query param extraction: meetingId
    url3 = "https://teams.microsoft.com/l/meetup-join/launch?meetingId=98765432101&p=TOKEN"
    sanitized3 = resolver.sanitize_meeting_url(url3, platform="MS_TEAMS")
    assert resolver.extract_meeting_code(sanitized3, None, platform="MS_TEAMS") == "98765432101"

    # 4. Traditional Meetup-join thread hashing
    raw_thread = "19%3ameeting_NzYwY2E4N2EtYzE2Mi00MTM1LThlNDAtYWE5Mjc4NjYwMTcw%40thread.v2"
    url4 = f"https://teams.microsoft.com/l/meetup-join/{raw_thread}/0?context=%7B%22Tid%22%3A%22test%22%7D"
    sanitized4 = resolver.sanitize_meeting_url(url4, platform="MS_TEAMS")
    expected_clean = unquote(raw_thread).strip().lower()
    expected_hash = hashlib.sha256(expected_clean.encode("utf-8")).hexdigest()[:16]
    extracted_code = resolver.extract_meeting_code(sanitized4, None, platform="MS_TEAMS")
    assert extracted_code == f"thread_{expected_hash}"

    # 5. Meeting title normalization
    assert resolver.normalize_meeting_title(None, "241890123456", platform="MS_TEAMS") == "Teams - 241890123456"
    assert resolver.normalize_meeting_title("Teams", "241890123456", platform="MS_TEAMS") == "Teams - 241890123456"
    assert resolver.normalize_meeting_title("Microsoft Teams", "241890123456", platform="MS_TEAMS") == "Teams - 241890123456"
    assert resolver.normalize_meeting_title("Teams Meeting", "241890123456", platform="MS_TEAMS") == "Teams - 241890123456"
    assert resolver.normalize_meeting_title("Teams Meeting (241890123456)", "241890123456", platform="MS_TEAMS") == "Teams - 241890123456"
    assert resolver.normalize_meeting_title("Microsoft Teams (241890123456)", "241890123456", platform="MS_TEAMS") == "Teams - 241890123456"
    assert resolver.normalize_meeting_title("Sprint Planning Daily | Microsoft Teams", "241890123456", platform="MS_TEAMS") == "Sprint Planning Daily"
    assert resolver.normalize_meeting_title("Sprint Planning Daily", "241890123456", platform="MS_TEAMS") == "Sprint Planning Daily"


def test_teams_shared_room_creation_and_multiuser_join_numeric(db_session: Session):
    uid_a = uuid.uuid4().hex[:6]
    uid_b = uuid.uuid4().hex[:6]
    user_a = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid_a}",
        email=f"teamsa_{uid_a}@example.com",
        display_name="Teams User A",
    )
    user_b = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid_b}",
        email=f"teamsb_{uid_b}@example.com",
        display_name="Teams User B",
    )
    db_session.add_all([user_a, user_b])
    db_session.commit()

    resolver = ConferenceIdentityResolver(db_session)
    access_repo = MeetingAccessRepository(db_session)

    teams_url = "https://teams.microsoft.com/meet/241890123456?p=SECRET_CREDENTIAL"

    # User A starts the Teams meeting
    m1, is_created_1 = resolver.resolve_or_create_shared_meeting(
        user_id=user_a.id,
        meeting_url=teams_url,
        title="",
        platform="MS_TEAMS",
    )

    assert is_created_1 is True
    assert m1.platform == "MS_TEAMS"
    assert m1.conference_identity == "teams:241890123456"
    assert m1.meeting_code == "241890123456"
    assert m1.title == "Teams - 241890123456"
    assert "p=" not in m1.meeting_url
    assert access_repo.get_user_role(m1.id, user_a.id) == MeetingRole.OWNER.value

    # User B joins the same Teams meeting
    m2, is_created_2 = resolver.resolve_or_create_shared_meeting(
        user_id=user_b.id,
        meeting_url=teams_url,
        title="Teams",
        platform="MS_TEAMS",
    )

    assert is_created_2 is False
    assert m2.id == m1.id
    assert m2.platform == "MS_TEAMS"
    assert access_repo.get_user_role(m1.id, user_b.id) == MeetingRole.PARTICIPANT.value


def test_teams_shared_room_creation_and_multiuser_join_thread(db_session: Session):
    uid_a = uuid.uuid4().hex[:6]
    uid_b = uuid.uuid4().hex[:6]
    user_a = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid_a}",
        email=f"tthreada_{uid_a}@example.com",
        display_name="Thread User A",
    )
    user_b = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid_b}",
        email=f"tthreadb_{uid_b}@example.com",
        display_name="Thread User B",
    )
    db_session.add_all([user_a, user_b])
    db_session.commit()

    resolver = ConferenceIdentityResolver(db_session)
    access_repo = MeetingAccessRepository(db_session)

    raw_thread = "19%3ameeting_MDEyMzQ1Njc4OTAtYWJj%40thread.v2"
    teams_url = f"https://teams.microsoft.com/l/meetup-join/{raw_thread}/0?context=%7B%22Tid%22%3A%22test%22%7D"

    # User A creates room via meetup-join
    m1, is_created_1 = resolver.resolve_or_create_shared_meeting(
        user_id=user_a.id,
        meeting_url=teams_url,
        title="Weekly Project Standup | Microsoft Teams",
        platform="MS_TEAMS",
    )

    clean_thread = unquote(raw_thread).strip().lower()
    expected_hash = hashlib.sha256(clean_thread.encode("utf-8")).hexdigest()[:16]

    assert is_created_1 is True
    assert m1.platform == "MS_TEAMS"
    assert m1.conference_identity == f"teams:thread_{expected_hash}"
    assert len(m1.conference_identity) < 30
    assert m1.title == "Weekly Project Standup"
    assert access_repo.get_user_role(m1.id, user_a.id) == MeetingRole.OWNER.value

    # User B joins via the same meetup-join link
    m2, is_created_2 = resolver.resolve_or_create_shared_meeting(
        user_id=user_b.id,
        meeting_url=teams_url,
        title="Teams",
        platform="MS_TEAMS",
    )

    assert is_created_2 is False
    assert m2.id == m1.id
    assert m2.platform == "MS_TEAMS"
    assert access_repo.get_user_role(m1.id, user_b.id) == MeetingRole.PARTICIPANT.value


def test_platform_isolation_three_way(db_session: Session):
    uid = uuid.uuid4().hex[:6]
    user = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid}",
        email=f"iso3_{uid}@example.com",
        display_name="Three-Way User",
    )
    db_session.add(user)
    db_session.commit()

    resolver = ConferenceIdentityResolver(db_session)

    # 1. Google Meet with code "9998887776"
    meet_m, is_created_meet = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url="https://meet.google.com/abc-defg-hij",
        title="Google Meet Session",
        platform="GOOGLE_MEET",
    )
    assert is_created_meet is True
    assert meet_m.platform == "GOOGLE_MEET"
    assert meet_m.title == "Meet - abc-defg-hij"

    # 2. Zoom with numeric code "9998887776"
    zoom_m, is_created_zoom = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url="https://app.zoom.us/wc/9998887776/join",
        title="Zoom",
        platform="ZOOM_WEB",
    )
    assert is_created_zoom is True
    assert zoom_m.platform == "ZOOM_WEB"
    assert zoom_m.title == "Zoom - 9998887776"
    assert zoom_m.conference_identity == "zoom_9998887776"

    # 3. MS Teams with the same numeric code "9998887776"
    teams_m, is_created_teams = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url="https://teams.microsoft.com/meet/9998887776",
        title="Teams",
        platform="MS_TEAMS",
    )
    assert is_created_teams is True
    assert teams_m.platform == "MS_TEAMS"
    assert teams_m.title == "Teams - 9998887776"
    assert teams_m.conference_identity == "teams:9998887776"

    # Strict isolation: 3 unique IDs
    assert len({meet_m.id, zoom_m.id, teams_m.id}) == 3


def test_teams_orphan_cleanup_creates_new_meeting(db_session: Session):
    uid = uuid.uuid4().hex[:6]
    user = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid}",
        email=f"torphan_{uid}@example.com",
        display_name="Teams Orphan User",
    )
    db_session.add(user)
    db_session.commit()

    resolver = ConferenceIdentityResolver(db_session)
    teams_url = "https://teams.microsoft.com/meet/241890123456"

    # Step 1: User starts an earlier Teams meeting
    m1, is_created_1 = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url=teams_url,
        title="Dev Team Sync",
        platform="MS_TEAMS",
    )
    assert is_created_1 is True
    assert m1.status == "in_progress"

    # Simulate that m1 was created 15 minutes ago and has no active heartbeats in the last 60s
    past_time = datetime.now(timezone.utc) - timedelta(minutes=15)
    m1.created_at = past_time
    m1.last_heartbeat_at = past_time
    db_session.commit()

    # Step 2: User starts a new session on the same Teams URL
    m2, is_created_2 = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url=teams_url,
        title="Dev Team Sync",
        platform="MS_TEAMS",
    )

    # Verification: Old orphan meeting m1 was closed, new meeting m2 was created!
    assert is_created_2 is True
    assert m2.id != m1.id
    assert m2.status == "in_progress"

    # Check that m1 was cleanly transitioned to completed
    db_session.refresh(m1)
    assert m1.status == "completed"
    assert m1.end_time is not None


def test_teams_explicit_meeting_code_param(db_session: Session):
    uid = uuid.uuid4().hex[:6]
    user = User(
        id=uuid.uuid4(),
        google_user_id=f"gid_{uid}",
        email=f"texplicit_{uid}@example.com",
        display_name="Teams Explicit Code User",
    )
    db_session.add(user)
    db_session.commit()

    resolver = ConferenceIdentityResolver(db_session)
    m, is_created = resolver.resolve_or_create_shared_meeting(
        user_id=user.id,
        meeting_url="https://teams.microsoft.com/meet",
        title="Teams",
        platform="MS_TEAMS",
        meeting_code="241890123456",
    )
    assert is_created is True
    assert m.meeting_code == "241890123456"
    assert m.conference_identity == "teams:241890123456"
    assert m.title == "Teams - 241890123456"
