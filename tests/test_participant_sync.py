import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.google_meet import (
    GoogleMeetAuthenticationError,
    GoogleMeetClient,
    GoogleMeetClientError,
    GoogleMeetListResponse,
    GoogleMeetMalformedResponseError,
    GoogleMeetServerError,
)
from app.models import Meeting, Participant, User
from app.repositories import MeetingRepository, ParticipantRepository, UserRepository
from app.services.participant_sync import (
    MeetingNotFoundError,
    ParticipantSyncService,
)


def create_test_meeting(db_session: Session, suffix: str = "part1") -> Meeting:
    user = UserRepository(db_session).create(
        {
            "google_user_id": f"google-user-{suffix}",
            "email": f"user-{suffix}@example.com",
        }
    )
    return MeetingRepository(db_session).create(
        {
            "user_id": user.id,
            "conference_record_name": f"conferenceRecords/conf-{suffix}",
            "title": f"Test Meeting {suffix}",
        }
    )


def test_sync_participants_creates_new_participants(db_session: Session) -> None:
    meeting = create_test_meeting(db_session, "new")
    participant_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    mock_client.list_participants.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-new/participants/p1",
                "earliestStartTime": "2026-08-26T09:00:00Z",
                "latestEndTime": "2026-08-26T09:45:00Z",
                "signedinUser": {
                    "user": "alice@example.com",
                    "displayName": "Alice Smith",
                },
            },
            {
                "name": "conferenceRecords/conf-new/participants/p2",
                "earliestStartTime": "2026-08-26T09:05:00Z",
                "latestEndTime": "2026-08-26T09:30:00Z",
                "anonymousUser": {
                    "displayName": "Bob Guest",
                },
            },
        ],
        next_page_token=None,
        raw={},
    )

    service = ParticipantSyncService(
        participant_repository=participant_repo,
        google_meet_client=mock_client,
    )

    synced = service.sync_participants(
        conference_record_name="conferenceRecords/conf-new",
        meeting_id=meeting.id,
    )

    assert len(synced) == 2
    mock_client.list_participants.assert_called_once_with(
        conference_record_name="conferenceRecords/conf-new",
        page_token=None,
    )

    p1 = synced[0]
    assert p1.meeting_id == meeting.id
    assert p1.google_participant_name == "conferenceRecords/conf-new/participants/p1"
    assert p1.participant_type == "signed_in_user"
    assert p1.display_name == "Alice Smith"
    assert p1.email == "alice@example.com"
    assert p1.join_time == datetime(2026, 8, 26, 9, 0, 0, tzinfo=timezone.utc)
    assert p1.leave_time == datetime(2026, 8, 26, 9, 45, 0, tzinfo=timezone.utc)

    p2 = synced[1]
    assert p2.meeting_id == meeting.id
    assert p2.google_participant_name == "conferenceRecords/conf-new/participants/p2"
    assert p2.participant_type == "anonymous_user"
    assert p2.display_name == "Bob Guest"
    assert p2.email is None

    total_count = db_session.scalar(select(func.count(Participant.id)))
    assert total_count == 2


def test_sync_participants_handles_pagination(db_session: Session) -> None:
    meeting = create_test_meeting(db_session, "page")
    participant_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    mock_client.list_participants.side_effect = [
        GoogleMeetListResponse(
            items=[
                {
                    "name": "conferenceRecords/conf-page/participants/p1",
                    "signedinUser": {"displayName": "Page 1 User"},
                }
            ],
            next_page_token="token-page-2",
            raw={},
        ),
        GoogleMeetListResponse(
            items=[
                {
                    "name": "conferenceRecords/conf-page/participants/p2",
                    "phoneUser": {"displayName": "+1234567890"},
                }
            ],
            next_page_token=None,
            raw={},
        ),
    ]

    service = ParticipantSyncService(
        participant_repository=participant_repo,
        google_meet_client=mock_client,
    )

    synced = service.sync_participants(
        conference_record_name="conferenceRecords/conf-page",
        meeting_id=meeting.id,
    )

    assert len(synced) == 2
    assert mock_client.list_participants.call_count == 2
    mock_client.list_participants.assert_any_call(
        conference_record_name="conferenceRecords/conf-page",
        page_token=None,
    )
    mock_client.list_participants.assert_any_call(
        conference_record_name="conferenceRecords/conf-page",
        page_token="token-page-2",
    )

    assert synced[0].display_name == "Page 1 User"
    assert synced[1].display_name == "+1234567890"
    assert synced[1].participant_type == "phone_user"


def test_sync_participants_idempotent_update(db_session: Session) -> None:
    meeting = create_test_meeting(db_session, "idem")
    participant_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    # Initial sync
    mock_client.list_participants.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-idem/participants/p1",
                "signedinUser": {"displayName": "User Initial"},
            }
        ],
        next_page_token=None,
        raw={},
    )

    service = ParticipantSyncService(
        participant_repository=participant_repo,
        google_meet_client=mock_client,
    )

    synced_1 = service.sync_participants(
        conference_record_name="conferenceRecords/conf-idem",
        meeting_id=meeting.id,
    )
    assert synced_1[0].display_name == "User Initial"

    # Subsequent sync with updated display_name
    mock_client.list_participants.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-idem/participants/p1",
                "signedinUser": {
                    "displayName": "User Updated",
                    "email": "user.updated@example.com",
                },
            }
        ],
        next_page_token=None,
        raw={},
    )

    synced_2 = service.sync_participants(
        conference_record_name="conferenceRecords/conf-idem",
        meeting_id=meeting.id,
    )

    assert len(synced_2) == 1
    assert synced_2[0].id == synced_1[0].id
    assert synced_2[0].display_name == "User Updated"
    assert synced_2[0].email == "user.updated@example.com"

    count = db_session.scalar(
        select(func.count(Participant.id)).where(
            Participant.meeting_id == meeting.id
        )
    )
    assert count == 1


def test_sync_participants_composite_identity(db_session: Session) -> None:
    meeting_1 = create_test_meeting(db_session, "comp1")
    meeting_2 = create_test_meeting(db_session, "comp2")
    participant_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    same_participant_name = "conferenceRecords/shared/participants/p1"

    # Sync for meeting 1
    mock_client.list_participants.return_value = GoogleMeetListResponse(
        items=[{"name": same_participant_name, "signedinUser": {"displayName": "M1 User"}}],
        next_page_token=None,
        raw={},
    )

    service = ParticipantSyncService(
        participant_repository=participant_repo,
        google_meet_client=mock_client,
    )

    p1 = service.sync_participants("conf-1", meeting_1.id)[0]

    # Sync for meeting 2
    mock_client.list_participants.return_value = GoogleMeetListResponse(
        items=[{"name": same_participant_name, "signedinUser": {"displayName": "M2 User"}}],
        next_page_token=None,
        raw={},
    )

    p2 = service.sync_participants("conf-2", meeting_2.id)[0]

    assert p1.id != p2.id
    assert p1.meeting_id == meeting_1.id
    assert p2.meeting_id == meeting_2.id
    assert p1.google_participant_name == p2.google_participant_name == same_participant_name


def test_sync_participants_preserves_existing_fields_on_missing_optional_data(
    db_session: Session,
) -> None:
    meeting = create_test_meeting(db_session, "pres")
    participant_repo = ParticipantRepository(db_session)

    # Pre-existing participant record with email
    existing = participant_repo.create(
        {
            "meeting_id": meeting.id,
            "google_participant_name": "conferenceRecords/conf-pres/participants/p1",
            "display_name": "Existing User",
            "email": "existing@example.com",
            "participant_type": "signed_in_user",
        }
    )

    mock_client = MagicMock(spec=GoogleMeetClient)
    # Response has signedinUser with displayName but no email key
    mock_client.list_participants.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-pres/participants/p1",
                "signedinUser": {"displayName": "Existing User"},
            }
        ],
        next_page_token=None,
        raw={},
    )

    service = ParticipantSyncService(
        participant_repository=participant_repo,
        google_meet_client=mock_client,
    )

    synced = service.sync_participants("conf-pres", meeting.id)

    assert synced[0].id == existing.id
    assert synced[0].email == "existing@example.com"


def test_sync_participants_all_participant_types(db_session: Session) -> None:
    meeting = create_test_meeting(db_session, "types")
    participant_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    mock_client.list_participants.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conf/participants/p1",
                "signedinUser": {"displayName": "User A"},
            },
            {
                "name": "conf/participants/p2",
                "anonymousUser": {"displayName": "Anon B"},
            },
            {
                "name": "conf/participants/p3",
                "phoneUser": {"displayName": "Phone C"},
            },
            {
                "name": "conf/participants/p4",
                "displayName": "Unknown D",
            },
        ],
        next_page_token=None,
        raw={},
    )

    service = ParticipantSyncService(
        participant_repository=participant_repo,
        google_meet_client=mock_client,
    )

    synced = service.sync_participants("conf", meeting.id)
    types = [p.participant_type for p in synced]

    assert types == ["signed_in_user", "anonymous_user", "phone_user", "unknown"]


def test_sync_participants_error_handling(db_session: Session) -> None:
    meeting = create_test_meeting(db_session, "err")
    participant_repo = ParticipantRepository(db_session)
    meeting_repo = MeetingRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    service = ParticipantSyncService(
        participant_repository=participant_repo,
        google_meet_client=mock_client,
        meeting_repository=meeting_repo,
    )

    # 1. Meeting not found error
    non_existent_id = uuid.uuid4()
    with pytest.raises(MeetingNotFoundError):
        service.sync_participants("conf", non_existent_id)

    # 2. Auth Error
    mock_client.list_participants.side_effect = GoogleMeetAuthenticationError(
        "Auth failed"
    )
    with pytest.raises(GoogleMeetAuthenticationError):
        service.sync_participants("conf", meeting.id)

    # 3. 403 / 404 Client Error
    mock_client.list_participants.side_effect = GoogleMeetClientError(404, "Not found")
    with pytest.raises(GoogleMeetClientError) as exc_404:
        service.sync_participants("conf", meeting.id)
    assert exc_404.value.status_code == 404

    # 4. 500 Server Error
    mock_client.list_participants.side_effect = GoogleMeetServerError(500, "Server error")
    with pytest.raises(GoogleMeetServerError) as exc_500:
        service.sync_participants("conf", meeting.id)
    assert exc_500.value.status_code == 500

    # 5. Malformed item error (missing name)
    mock_client.list_participants.side_effect = None
    mock_client.list_participants.return_value = GoogleMeetListResponse(
        items=[{"signedinUser": {"displayName": "No Name"}}],
        next_page_token=None,
        raw={},
    )
    with pytest.raises(GoogleMeetMalformedResponseError):
        service.sync_participants("conf", meeting.id)
