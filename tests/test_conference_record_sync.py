import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.google_meet import (
    GoogleMeetAuthenticationError,
    GoogleMeetClient,
    GoogleMeetClientError,
    GoogleMeetMalformedResponseError,
    GoogleMeetServerError,
)
from app.models import Meeting, User
from app.repositories import MeetingRepository, UserRepository
from app.services.conference_record_sync import (
    ConferenceRecordSyncService,
    parse_iso_datetime,
)


def create_test_user(db_session: Session, suffix: str = "sync1") -> User:
    return UserRepository(db_session).create(
        {
            "google_user_id": f"google-user-{suffix}",
            "email": f"user-{suffix}@example.com",
        }
    )


def test_parse_iso_datetime_handles_various_formats() -> None:
    # ISO 8601 with Z
    dt1 = parse_iso_datetime("2026-08-25T10:00:00Z")
    assert dt1 == datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)

    # ISO 8601 with timezone offset
    dt2 = parse_iso_datetime("2026-08-25T10:00:00+07:00")
    assert dt2 is not None
    assert dt2.tzinfo is not None

    # Invalid / Empty / None
    assert parse_iso_datetime("") is None
    assert parse_iso_datetime(None) is None
    assert parse_iso_datetime("invalid-date") is None
    assert parse_iso_datetime(12345) is None


def test_sync_conference_record_creates_new_meeting(db_session: Session) -> None:
    user = create_test_user(db_session, "create")
    meeting_repo = MeetingRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    mock_client.get_conference_record.return_value = {
        "name": "conferenceRecords/conf-new-123",
        "space": "spaces/space-abc",
        "startTime": "2026-08-25T09:00:00Z",
        "endTime": "2026-08-25T10:00:00Z",
    }

    service = ConferenceRecordSyncService(
        meeting_repository=meeting_repo,
        google_meet_client=mock_client,
    )

    meeting = service.sync_conference_record(
        conference_record_name="conferenceRecords/conf-new-123",
        user_id=user.id,
    )

    mock_client.get_conference_record.assert_called_once_with("conferenceRecords/conf-new-123")
    assert meeting.id is not None
    assert meeting.user_id == user.id
    assert meeting.conference_record_name == "conferenceRecords/conf-new-123"
    assert meeting.meeting_space_name == "spaces/space-abc"
    assert meeting.start_time == datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)
    assert meeting.end_time == datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)
    assert meeting.status == "in_progress"

    count = db_session.scalar(select(func.count(Meeting.id)))
    assert count == 1


def test_sync_conference_record_idempotent_update(db_session: Session) -> None:
    user = create_test_user(db_session, "idempotent")
    meeting_repo = MeetingRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    # Initial sync when meeting is ongoing
    mock_client.get_conference_record.return_value = {
        "name": "conferenceRecords/conf-idem-123",
        "space": "spaces/space-idem",
        "startTime": "2026-08-25T09:00:00Z",
    }

    service = ConferenceRecordSyncService(
        meeting_repository=meeting_repo,
        google_meet_client=mock_client,
    )

    meeting_1 = service.sync_conference_record(
        conference_record_name="conferenceRecords/conf-idem-123",
        user_id=user.id,
    )
    assert meeting_1.status == "in_progress"
    assert meeting_1.end_time is None

    # Subsequent sync when meeting ends (sync updates end_time but preserves lifecycle status)
    mock_client.get_conference_record.return_value = {
        "name": "conferenceRecords/conf-idem-123",
        "space": "spaces/space-idem",
        "startTime": "2026-08-25T09:00:00Z",
        "endTime": "2026-08-25T10:00:00Z",
    }

    meeting_2 = service.sync_conference_record(
        conference_record_name="conferenceRecords/conf-idem-123",
        user_id=user.id,
    )

    assert meeting_2.id == meeting_1.id
    assert meeting_2.status == "in_progress"
    assert meeting_2.end_time == datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)

    # Ensure no duplicates were created
    count = db_session.scalar(
        select(func.count(Meeting.id)).where(
            Meeting.conference_record_name == "conferenceRecords/conf-idem-123"
        )
    )
    assert count == 1


def test_sync_conference_record_preserves_local_fields(db_session: Session) -> None:
    user = create_test_user(db_session, "preserve")
    meeting_repo = MeetingRepository(db_session)

    # Pre-existing local record created e.g. by Extension
    existing = meeting_repo.create(
        {
            "user_id": user.id,
            "conference_record_name": "conferenceRecords/conf-preserve-123",
            "meeting_url": "https://meet.google.com/xyz-1234-abc",
            "title": "Local Custom Title",
            "dom_capture_status": "received",
            "ai_status": "NOT_PROCESSED",
        }
    )

    mock_client = MagicMock(spec=GoogleMeetClient)
    mock_client.get_conference_record.return_value = {
        "name": "conferenceRecords/conf-preserve-123",
        "space": "spaces/space-preserve",
        "startTime": "2026-08-25T09:00:00Z",
        "endTime": "2026-08-25T10:00:00Z",
    }

    service = ConferenceRecordSyncService(
        meeting_repository=meeting_repo,
        google_meet_client=mock_client,
    )

    synced = service.sync_conference_record(
        conference_record_name="conferenceRecords/conf-preserve-123",
        user_id=user.id,
    )

    assert synced.id == existing.id
    # Updated fields from API
    assert synced.meeting_space_name == "spaces/space-preserve"
    assert synced.start_time == datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)
    assert synced.end_time == datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)

    # Preserved local fields & status
    assert synced.status == "in_progress"

    # Preserved local fields
    assert synced.meeting_url == "https://meet.google.com/xyz-1234-abc"
    assert synced.title == "Local Custom Title"
    assert synced.dom_capture_status == "received"
    assert synced.ai_status == "NOT_PROCESSED"


def test_sync_conference_record_handles_auth_error(db_session: Session) -> None:
    user = create_test_user(db_session, "auth_err")
    meeting_repo = MeetingRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)
    mock_client.get_conference_record.side_effect = GoogleMeetAuthenticationError(
        "Google Meet authentication failed."
    )

    service = ConferenceRecordSyncService(
        meeting_repository=meeting_repo,
        google_meet_client=mock_client,
    )

    with pytest.raises(GoogleMeetAuthenticationError) as exc_info:
        service.sync_conference_record(
            conference_record_name="conferenceRecords/conf-err-123",
            user_id=user.id,
        )

    assert "authentication failed" in str(exc_info.value)
    # Ensure no record was created in DB
    count = db_session.scalar(select(func.count(Meeting.id)))
    assert count == 0


def test_sync_conference_record_handles_client_and_server_errors(db_session: Session) -> None:
    user = create_test_user(db_session, "err_cases")
    meeting_repo = MeetingRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    service = ConferenceRecordSyncService(
        meeting_repository=meeting_repo,
        google_meet_client=mock_client,
    )

    # 404 Client Error
    mock_client.get_conference_record.side_effect = GoogleMeetClientError(
        404, "Google Meet API client request failed."
    )

    with pytest.raises(GoogleMeetClientError) as exc_404:
        service.sync_conference_record(
            conference_record_name="conferenceRecords/nonexistent",
            user_id=user.id,
        )
    assert exc_404.value.status_code == 404

    # 500 Server Error
    mock_client.get_conference_record.side_effect = GoogleMeetServerError(
        500, "Google Meet API server request failed."
    )

    with pytest.raises(GoogleMeetServerError) as exc_500:
        service.sync_conference_record(
            conference_record_name="conferenceRecords/500-error",
            user_id=user.id,
        )
    assert exc_500.value.status_code == 500

    # Malformed Response Error
    mock_client.get_conference_record.side_effect = GoogleMeetMalformedResponseError(
        "Google Meet resource is malformed."
    )

    with pytest.raises(GoogleMeetMalformedResponseError):
        service.sync_conference_record(
            conference_record_name="conferenceRecords/malformed",
            user_id=user.id,
        )
