import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.google_meet import GoogleMeetClient, GoogleMeetListResponse
from app.models import Meeting, Participant, Transcript, TranscriptEntry, User
from app.repositories import (
    MeetingRepository,
    ParticipantRepository,
    TranscriptEntryRepository,
    TranscriptRepository,
    UserRepository,
)
from app.services.conference_record_sync import ConferenceRecordSyncService
from app.services.participant_sync import ParticipantSyncService
from app.services.transcript_sync import TranscriptSyncService


def test_phase2_end_to_end_integration_flow(db_session: Session) -> None:
    # 0. Setup test user
    user = UserRepository(db_session).create(
        {
            "google_user_id": "google-user-phase2-e2e",
            "email": "e2e@example.com",
        }
    )

    conf_name = "conferenceRecords/conf-e2e-999"
    part_name_1 = f"{conf_name}/participants/p1"
    part_name_2 = f"{conf_name}/participants/p2"
    tx_name = f"{conf_name}/transcripts/t1"
    entry_name_1 = f"{tx_name}/entries/e1"
    entry_name_2 = f"{tx_name}/entries/e2"

    mock_client = MagicMock(spec=GoogleMeetClient)

    # Mock 1: Conference Record
    mock_client.get_conference_record.return_value = {
        "name": conf_name,
        "space": "spaces/space-e2e",
        "startTime": "2026-08-26T08:00:00Z",
        "endTime": "2026-08-26T09:00:00Z",
    }

    # Mock 2: Participants
    mock_client.list_participants.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": part_name_1,
                "signedinUser": {
                    "user": "alice@example.com",
                    "displayName": "Alice E2E",
                },
                "earliestStartTime": "2026-08-26T08:00:00Z",
                "latestEndTime": "2026-08-26T08:50:00Z",
            },
            {
                "name": part_name_2,
                "anonymousUser": {
                    "displayName": "Bob Guest",
                },
                "earliestStartTime": "2026-08-26T08:10:00Z",
                "latestEndTime": "2026-08-26T08:40:00Z",
            },
        ],
        next_page_token=None,
        raw={},
    )

    # Mock 3: Transcripts
    mock_client.list_transcripts.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": tx_name,
                "state": "FILE_GENERATED",
                "docsDestination": {
                    "document": "https://docs.google.com/document/d/e2e-doc/edit"
                },
            }
        ],
        next_page_token=None,
        raw={},
    )

    # Mock 4: Transcript Entries
    mock_client.list_transcript_entries.return_value = GoogleMeetListResponse(
        items=[
            {
                "name": entry_name_1,
                "participant": part_name_1,
                "text": "Hello, this is Alice speaking in E2E test.",
                "languageCode": "en",
                "startTime": "2026-08-26T08:01:00Z",
                "endTime": "2026-08-26T08:01:10Z",
            },
            {
                "name": entry_name_2,
                "participant": "conferenceRecords/conf-e2e-999/participants/unknown_p",
                "text": "Hello, this is an unresolved speaker.",
                "languageCode": "en",
                "startTime": "2026-08-26T08:02:00Z",
                "endTime": "2026-08-26T08:02:05Z",
            },
        ],
        next_page_token=None,
        raw={},
    )

    # Instantiate repositories
    meeting_repo = MeetingRepository(db_session)
    participant_repo = ParticipantRepository(db_session)
    tx_repo = TranscriptRepository(db_session)
    entry_repo = TranscriptEntryRepository(db_session)

    # Instantiate services
    conf_sync_service = ConferenceRecordSyncService(meeting_repo, mock_client)
    part_sync_service = ParticipantSyncService(participant_repo, mock_client, meeting_repo)
    tx_sync_service = TranscriptSyncService(
        tx_repo, entry_repo, participant_repo, mock_client, meeting_repo, db_session
    )

    # Step A: Sync Conference Record -> Meeting
    meeting = conf_sync_service.sync_conference_record(conf_name, user.id)
    assert meeting.conference_record_name == conf_name
    assert meeting.meeting_space_name == "spaces/space-e2e"
    assert meeting.start_time == datetime(2026, 8, 26, 8, 0, 0, tzinfo=timezone.utc)

    # Step B: Sync Participants
    synced_parts = part_sync_service.sync_participants(conf_name, meeting.id)
    assert len(synced_parts) == 2
    alice_part = next(p for p in synced_parts if p.google_participant_name == part_name_1)
    assert alice_part.display_name == "Alice E2E"
    assert alice_part.email == "alice@example.com"

    # Step C: Sync Transcripts & Entries + Speaker Mapping
    synced_transcripts = tx_sync_service.sync_transcripts(conf_name, meeting.id)
    assert len(synced_transcripts) == 1
    transcript = synced_transcripts[0]
    assert transcript.processing_status == "completed"

    entries = list(db_session.scalars(select(TranscriptEntry)))
    assert len(entries) == 2

    # Speaker mapping verification
    alice_entry = next(e for e in entries if e.google_entry_name == entry_name_1)
    assert alice_entry.participant_id == alice_part.id

    unresolved_entry = next(e for e in entries if e.google_entry_name == entry_name_2)
    assert unresolved_entry.participant_id is None

    # Step D: Re-run entire sync pipeline (Idempotency verification)
    meeting_2 = conf_sync_service.sync_conference_record(conf_name, user.id)
    synced_parts_2 = part_sync_service.sync_participants(conf_name, meeting.id)
    synced_tx_2 = tx_sync_service.sync_transcripts(conf_name, meeting.id)

    assert meeting_2.id == meeting.id
    assert len(synced_parts_2) == 2
    assert len(synced_tx_2) == 1

    # Verify counts in DB remain unchanged (0 duplicates created)
    meeting_count = db_session.scalar(select(func.count(Meeting.id)))
    participant_count = db_session.scalar(select(func.count(Participant.id)))
    tx_count = db_session.scalar(select(func.count(Transcript.id)))
    entry_count = db_session.scalar(select(func.count(TranscriptEntry.id)))

    assert meeting_count == 1
    assert participant_count == 2
    assert tx_count == 1
    assert entry_count == 2
