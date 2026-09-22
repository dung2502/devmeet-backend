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
from app.models import Meeting, Participant, Transcript, TranscriptEntry, User
from app.repositories import (
    MeetingRepository,
    ParticipantRepository,
    TranscriptEntryRepository,
    TranscriptRepository,
    UserRepository,
)
from app.services.transcript_sync import (
    MeetingNotFoundError,
    TranscriptNotReadyError,
    TranscriptSyncService,
    calculate_transcript_retry_backoff,
)


def create_test_setup(
    db_session: Session, suffix: str = "tx1"
) -> tuple[User, Meeting, Participant]:
    user = UserRepository(db_session).create(
        {
            "google_user_id": f"google-user-{suffix}",
            "email": f"user-{suffix}@example.com",
        }
    )
    meeting = MeetingRepository(db_session).create(
        {
            "user_id": user.id,
            "conference_record_name": f"conferenceRecords/conf-{suffix}",
            "title": f"Test Meeting {suffix}",
        }
    )
    participant = ParticipantRepository(db_session).create(
        {
            "meeting_id": meeting.id,
            "google_participant_name": f"conferenceRecords/conf-{suffix}/participants/p1",
            "display_name": "Speaker One",
            "participant_type": "signed_in_user",
        }
    )
    return user, meeting, participant


def test_calculate_transcript_retry_backoff() -> None:
    assert calculate_transcript_retry_backoff(1) == 10.0
    assert calculate_transcript_retry_backoff(2) == 30.0
    assert calculate_transcript_retry_backoff(3) == 60.0
    assert calculate_transcript_retry_backoff(4) == 120.0


def test_sync_transcripts_state_started_raises_not_ready(db_session: Session) -> None:
    _, meeting, _ = create_test_setup(db_session, "started")
    tx_repo = TranscriptRepository(db_session)
    entry_repo = TranscriptEntryRepository(db_session)
    part_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    mock_client.list_transcripts.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-started/transcripts/t1",
                "state": "STARTED",
            }
        ],
        next_page_token=None,
        raw={},
    )

    service = TranscriptSyncService(
        transcript_repository=tx_repo,
        transcript_entry_repository=entry_repo,
        participant_repository=part_repo,
        google_meet_client=mock_client,
        session=db_session,
    )

    with pytest.raises(TranscriptNotReadyError) as exc_info:
        service.sync_transcripts("conferenceRecords/conf-started", meeting.id)

    assert exc_info.value.status_code == 400
    assert exc_info.value.error_code == "TRANSCRIPT_NOT_READY"
    assert exc_info.value.state == "STARTED"

    # Verify transcript recorded with processing_status = "processing" and state = "STARTED"
    saved_tx = db_session.scalar(select(Transcript))
    assert saved_tx is not None
    assert saved_tx.state == "STARTED"
    assert saved_tx.processing_status == "processing"

    # Verify no transcript entries were created
    entry_count = db_session.scalar(select(func.count(TranscriptEntry.id)))
    assert entry_count == 0
    mock_client.list_transcript_entries.assert_not_called()


def test_sync_transcripts_state_ended_raises_not_ready(db_session: Session) -> None:
    _, meeting, _ = create_test_setup(db_session, "ended")
    tx_repo = TranscriptRepository(db_session)
    entry_repo = TranscriptEntryRepository(db_session)
    part_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    mock_client.list_transcripts.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-ended/transcripts/t1",
                "state": "ENDED",
            }
        ],
        next_page_token=None,
        raw={},
    )

    service = TranscriptSyncService(
        transcript_repository=tx_repo,
        transcript_entry_repository=entry_repo,
        participant_repository=part_repo,
        google_meet_client=mock_client,
        session=db_session,
    )

    with pytest.raises(TranscriptNotReadyError) as exc_info:
        service.sync_transcripts("conferenceRecords/conf-ended", meeting.id)

    assert exc_info.value.state == "ENDED"
    mock_client.list_transcript_entries.assert_not_called()


def test_sync_transcripts_file_generated_success(db_session: Session) -> None:
    _, meeting, participant = create_test_setup(db_session, "success")
    tx_repo = TranscriptRepository(db_session)
    entry_repo = TranscriptEntryRepository(db_session)
    part_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    mock_client.list_transcripts.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-success/transcripts/t1",
                "state": "FILE_GENERATED",
                "docsDestination": {
                    "document": "https://docs.google.com/document/d/doc-123/edit"
                },
            }
        ],
        next_page_token=None,
        raw={},
    )

    mock_client.list_transcript_entries.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-success/transcripts/t1/entries/e1",
                "participant": participant.google_participant_name,
                "text": "Hello world from official transcript",
                "languageCode": "en-US",
                "startTime": "2026-08-26T09:00:00Z",
                "endTime": "2026-08-26T09:00:05Z",
            }
        ],
        next_page_token=None,
        raw={},
    )

    service = TranscriptSyncService(
        transcript_repository=tx_repo,
        transcript_entry_repository=entry_repo,
        participant_repository=part_repo,
        google_meet_client=mock_client,
        session=db_session,
    )

    synced_transcripts = service.sync_transcripts(
        "conferenceRecords/conf-success", meeting.id
    )

    assert len(synced_transcripts) == 1
    tx = synced_transcripts[0]
    assert tx.meeting_id == meeting.id
    assert tx.google_transcript_name == "conferenceRecords/conf-success/transcripts/t1"
    assert tx.state == "FILE_GENERATED"
    assert tx.docs_url == "https://docs.google.com/document/d/doc-123/edit"
    assert tx.processing_status == "completed"
    assert tx.fetched_at is not None

    entries = list(db_session.scalars(select(TranscriptEntry)))
    assert len(entries) == 1
    e1 = entries[0]
    assert e1.transcript_id == tx.id
    assert e1.participant_id == participant.id
    assert e1.google_entry_name == "conferenceRecords/conf-success/transcripts/t1/entries/e1"
    assert e1.text == "Hello world from official transcript"
    assert e1.language_code == "en-US"
    assert e1.start_time == datetime(2026, 8, 26, 9, 0, 0, tzinfo=timezone.utc)
    assert e1.end_time == datetime(2026, 8, 26, 9, 0, 5, tzinfo=timezone.utc)
    assert e1.source == "google_meet"


def test_sync_transcripts_speaker_mapping_unresolved(db_session: Session) -> None:
    _, meeting, _ = create_test_setup(db_session, "unresolved")
    tx_repo = TranscriptRepository(db_session)
    entry_repo = TranscriptEntryRepository(db_session)
    part_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    mock_client.list_transcripts.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-unresolved/transcripts/t1",
                "state": "FILE_GENERATED",
            }
        ],
        next_page_token=None,
        raw={},
    )

    # Entry participant does not exist in DB
    mock_client.list_transcript_entries.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-unresolved/transcripts/t1/entries/e1",
                "participant": "conferenceRecords/conf-unresolved/participants/unknown_speaker",
                "text": "Unresolved speaker statement",
            }
        ],
        next_page_token=None,
        raw={},
    )

    service = TranscriptSyncService(
        transcript_repository=tx_repo,
        transcript_entry_repository=entry_repo,
        participant_repository=part_repo,
        google_meet_client=mock_client,
        session=db_session,
    )

    synced_transcripts = service.sync_transcripts(
        "conferenceRecords/conf-unresolved", meeting.id
    )

    assert len(synced_transcripts) == 1
    entries = list(db_session.scalars(select(TranscriptEntry)))
    assert len(entries) == 1
    assert entries[0].participant_id is None
    assert entries[0].text == "Unresolved speaker statement"


def test_sync_transcripts_pagination_for_transcripts_and_entries(
    db_session: Session,
) -> None:
    _, meeting, participant = create_test_setup(db_session, "page")
    tx_repo = TranscriptRepository(db_session)
    entry_repo = TranscriptEntryRepository(db_session)
    part_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    # Transcripts pagination: 2 pages
    mock_client.list_transcripts.side_effect = [
        GoogleMeetListResponse(
            items=[
                {
                    "name": "conferenceRecords/conf-page/transcripts/t1",
                    "state": "FILE_GENERATED",
                }
            ],
            next_page_token="tx-token-2",
            raw={},
        ),
        GoogleMeetListResponse(
            items=[
                {
                    "name": "conferenceRecords/conf-page/transcripts/t2",
                    "state": "FILE_GENERATED",
                }
            ],
            next_page_token=None,
            raw={},
        ),
    ]

    # Entries pagination for t1: 2 pages
    # Entries pagination for t2: 1 page
    mock_client.list_transcript_entries.side_effect = [
        GoogleMeetListResponse(
            items=[
                {
                    "name": "t1/entries/e1",
                    "participant": participant.google_participant_name,
                    "text": "t1 e1",
                }
            ],
            next_page_token="entry-token-2",
            raw={},
        ),
        GoogleMeetListResponse(
            items=[
                {
                    "name": "t1/entries/e2",
                    "participant": participant.google_participant_name,
                    "text": "t1 e2",
                }
            ],
            next_page_token=None,
            raw={},
        ),
        GoogleMeetListResponse(
            items=[
                {
                    "name": "t2/entries/e1",
                    "participant": participant.google_participant_name,
                    "text": "t2 e1",
                }
            ],
            next_page_token=None,
            raw={},
        ),
    ]

    service = TranscriptSyncService(
        transcript_repository=tx_repo,
        transcript_entry_repository=entry_repo,
        participant_repository=part_repo,
        google_meet_client=mock_client,
        session=db_session,
    )

    synced = service.sync_transcripts("conferenceRecords/conf-page", meeting.id)

    assert len(synced) == 2
    assert mock_client.list_transcripts.call_count == 2
    assert mock_client.list_transcript_entries.call_count == 3

    total_entries = db_session.scalar(select(func.count(TranscriptEntry.id)))
    assert total_entries == 3


def test_sync_transcripts_idempotency(db_session: Session) -> None:
    _, meeting, participant = create_test_setup(db_session, "idem")
    tx_repo = TranscriptRepository(db_session)
    entry_repo = TranscriptEntryRepository(db_session)
    part_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    mock_client.list_transcripts.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-idem/transcripts/t1",
                "state": "FILE_GENERATED",
            }
        ],
        next_page_token=None,
        raw={},
    )
    mock_client.list_transcript_entries.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "t1/entries/e1",
                "participant": participant.google_participant_name,
                "text": "Initial text",
            }
        ],
        next_page_token=None,
        raw={},
    )

    service = TranscriptSyncService(
        transcript_repository=tx_repo,
        transcript_entry_repository=entry_repo,
        participant_repository=part_repo,
        google_meet_client=mock_client,
        session=db_session,
    )

    synced_1 = service.sync_transcripts("conferenceRecords/conf-idem", meeting.id)
    assert synced_1[0].processing_status == "completed"

    # Second sync with updated entry text
    mock_client.list_transcript_entries.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "t1/entries/e1",
                "participant": participant.google_participant_name,
                "text": "Updated text",
            }
        ],
        next_page_token=None,
        raw={},
    )

    synced_2 = service.sync_transcripts("conferenceRecords/conf-idem", meeting.id)

    assert synced_2[0].id == synced_1[0].id

    tx_count = db_session.scalar(select(func.count(Transcript.id)))
    entry_count = db_session.scalar(select(func.count(TranscriptEntry.id)))
    assert tx_count == 1
    assert entry_count == 1

    entry = db_session.scalar(select(TranscriptEntry))
    assert entry is not None
    assert entry.text == "Updated text"


def test_sync_transcripts_transaction_failure_handling(db_session: Session) -> None:
    _, meeting, participant = create_test_setup(db_session, "rollback")
    tx_repo = TranscriptRepository(db_session)
    entry_repo = TranscriptEntryRepository(db_session)
    part_repo = ParticipantRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    mock_client.list_transcripts.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": "conferenceRecords/conf-rollback/transcripts/t1",
                "state": "FILE_GENERATED",
            }
        ],
        next_page_token=None,
        raw={},
    )

    # API error when fetching transcript entries
    mock_client.list_transcript_entries.side_effect = GoogleMeetServerError(
        500, "Google Meet API error"
    )

    service = TranscriptSyncService(
        transcript_repository=tx_repo,
        transcript_entry_repository=entry_repo,
        participant_repository=part_repo,
        google_meet_client=mock_client,
        session=db_session,
    )

    with pytest.raises(GoogleMeetServerError):
        service.sync_transcripts("conferenceRecords/conf-rollback", meeting.id)

    # Verify transcript processing_status was updated to "failed"
    saved_tx = db_session.scalar(select(Transcript))
    assert saved_tx is not None
    assert saved_tx.processing_status == "failed"

    # Verify no transcript entries were created
    entry_count = db_session.scalar(select(func.count(TranscriptEntry.id)))
    assert entry_count == 0


def test_sync_transcripts_error_propagation(db_session: Session) -> None:
    _, meeting, _ = create_test_setup(db_session, "err")
    tx_repo = TranscriptRepository(db_session)
    entry_repo = TranscriptEntryRepository(db_session)
    part_repo = ParticipantRepository(db_session)
    meeting_repo = MeetingRepository(db_session)
    mock_client = MagicMock(spec=GoogleMeetClient)

    service = TranscriptSyncService(
        transcript_repository=tx_repo,
        transcript_entry_repository=entry_repo,
        participant_repository=part_repo,
        google_meet_client=mock_client,
        meeting_repository=meeting_repo,
        session=db_session,
    )

    # 1. Meeting not found error
    non_existent_id = uuid.uuid4()
    with pytest.raises(MeetingNotFoundError):
        service.sync_transcripts("conf", non_existent_id)

    # 2. Auth error (401)
    mock_client.list_transcripts.side_effect = GoogleMeetAuthenticationError(
        "Auth failed"
    )
    with pytest.raises(GoogleMeetAuthenticationError):
        service.sync_transcripts("conf", meeting.id)

    # 3. Client error (403/404)
    mock_client.list_transcripts.side_effect = GoogleMeetClientError(404, "Not found")
    with pytest.raises(GoogleMeetClientError) as exc_404:
        service.sync_transcripts("conf", meeting.id)
    assert exc_404.value.status_code == 404

    # 4. Malformed transcript response (missing state)
    mock_client.list_transcripts.side_effect = None
    mock_client.list_transcripts.return_value = GoogleMeetListResponse(
        items=[{"name": "t1"}],
        next_page_token=None,
        raw={},
    )
    with pytest.raises(GoogleMeetMalformedResponseError):
        service.sync_transcripts("conf", meeting.id)
