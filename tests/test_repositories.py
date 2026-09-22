import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Meeting, Participant, Transcript, TranscriptEntry, User
from app.repositories import (
    MeetingRepository,
    ParticipantRepository,
    TranscriptEntryRepository,
    TranscriptRepository,
    UserRepository,
)


def create_user(db_session: Session, suffix: str = "1") -> User:
    return UserRepository(db_session).create(
        {
            "google_user_id": f"google-user-{suffix}",
            "email": f"user-{suffix}@example.com",
        }
    )


def create_meeting(db_session: Session, suffix: str = "1") -> Meeting:
    user = create_user(db_session, suffix)
    return MeetingRepository(db_session).create(
        {
            "user_id": user.id,
            "conference_record_name": f"conferenceRecords/{suffix}",
            "title": f"Meeting {suffix}",
        }
    )


def create_transcript(
    db_session: Session,
    meeting: Meeting,
    suffix: str = "1",
) -> Transcript:
    return TranscriptRepository(db_session).create(
        {
            "meeting_id": meeting.id,
            "google_transcript_name": f"conferenceRecords/1/transcripts/{suffix}",
            "state": "FILE_GENERATED",
        }
    )


def test_user_repository_create_get_update_and_unique_constraints(
    db_session: Session,
) -> None:
    repository = UserRepository(db_session)
    user = repository.create(
        {
            "google_user_id": "google-user-1",
            "email": "user-1@example.com",
            "display_name": "User One",
        }
    )

    assert repository.get_by_id(user.id) == user

    updated = repository.update(user.id, {"display_name": "Updated User"})
    assert updated is not None
    assert updated.display_name == "Updated User"

    assert repository.update(uuid.uuid4(), {"display_name": "Missing"}) is None

    with pytest.raises(IntegrityError):
        repository.create(
            {
                "google_user_id": "google-user-1",
                "email": "other@example.com",
            }
        )
    db_session.rollback()

    with pytest.raises(IntegrityError):
        repository.create(
            {
                "google_user_id": "google-user-2",
                "email": "user-1@example.com",
            }
        )


def test_user_repository_upsert_uses_google_user_id_without_duplicate(
    db_session: Session,
) -> None:
    repository = UserRepository(db_session)

    created = repository.upsert(
        {
            "google_user_id": "google-user-1",
            "email": "user-1@example.com",
            "display_name": "Original",
        }
    )
    updated = repository.upsert(
        {
            "google_user_id": "google-user-1",
            "email": "user-1-new@example.com",
            "display_name": "Updated",
        }
    )

    assert updated.id == created.id
    assert updated.email == "user-1-new@example.com"
    assert updated.display_name == "Updated"
    assert db_session.scalar(select(func.count()).select_from(User)) == 1


def test_user_repository_list_with_pagination_is_deterministic(
    db_session: Session,
) -> None:
    repository = UserRepository(db_session)
    users = [
        repository.create(
            {
                "google_user_id": f"google-user-{index}",
                "email": f"user-{index}@example.com",
            }
        )
        for index in range(3)
    ]

    first_page = repository.list_with_pagination(page=1, page_size=2)
    second_page = repository.list_with_pagination(page=2, page_size=2)

    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {user.id for user in first_page + second_page} == {user.id for user in users}
    assert repository.list_with_pagination(page=1, page_size=2) == first_page

    with pytest.raises(ValueError):
        repository.list_with_pagination(page=0)
    with pytest.raises(ValueError):
        repository.list_with_pagination(page_size=0)


def test_meeting_repository_create_get_update_relationship_and_unique_constraint(
    db_session: Session,
) -> None:
    user = UserRepository(db_session).create(
        {
            "google_user_id": "google-user-1",
            "email": "user-1@example.com",
        }
    )
    repository = MeetingRepository(db_session)
    meeting = repository.create(
        {
            "user_id": user.id,
            "conference_record_name": "conferenceRecords/1",
            "title": "Original title",
        }
    )

    found = repository.get_by_id(meeting.id)
    assert found == meeting
    assert found is not None
    assert found.user == user

    updated = repository.update(
        meeting.id,
        {"title": "Updated title", "ai_status": "PROCESSING"},
    )
    assert updated is not None
    assert updated.title == "Updated title"
    assert updated.ai_status == "PROCESSING"

    assert repository.update(uuid.uuid4(), {"title": "Missing"}) is None

    with pytest.raises(IntegrityError):
        repository.create(
            {
                "user_id": user.id,
                "conference_record_name": "conferenceRecords/1",
            }
        )


def test_meeting_repository_rejects_missing_user_fk(db_session: Session) -> None:
    repository = MeetingRepository(db_session)

    with pytest.raises(IntegrityError):
        repository.create(
            {
                "user_id": uuid.uuid4(),
                "conference_record_name": "conferenceRecords/missing-user",
            }
        )


def test_meeting_repository_upsert_uses_conference_record_name_without_duplicate(
    db_session: Session,
) -> None:
    user_repository = UserRepository(db_session)
    first_user = user_repository.create(
        {
            "google_user_id": "google-user-1",
            "email": "user-1@example.com",
        }
    )
    second_user = user_repository.create(
        {
            "google_user_id": "google-user-2",
            "email": "user-2@example.com",
        }
    )
    repository = MeetingRepository(db_session)

    created = repository.upsert(
        {
            "user_id": first_user.id,
            "conference_record_name": "conferenceRecords/1",
            "title": "Original title",
        }
    )
    updated = repository.upsert(
        {
            "user_id": second_user.id,
            "conference_record_name": "conferenceRecords/1",
            "title": "Updated title",
            "sheets_sync_status": "SYNCING",
        }
    )

    assert updated.id == created.id
    assert updated.user_id == second_user.id
    assert updated.title == "Updated title"
    assert updated.sheets_sync_status == "SYNCING"
    assert db_session.scalar(select(func.count()).select_from(Meeting)) == 1


def test_meeting_repository_list_with_pagination_is_deterministic(
    db_session: Session,
) -> None:
    user = UserRepository(db_session).create(
        {
            "google_user_id": "google-user-1",
            "email": "user-1@example.com",
        }
    )
    repository = MeetingRepository(db_session)
    meetings = [
        repository.create(
            {
                "user_id": user.id,
                "conference_record_name": f"conferenceRecords/{index}",
                "title": f"Meeting {index}",
            }
        )
        for index in range(3)
    ]

    first_page = repository.list_with_pagination(page=1, page_size=2)
    second_page = repository.list_with_pagination(page=2, page_size=2)

    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {meeting.id for meeting in first_page + second_page} == {
        meeting.id for meeting in meetings
    }
    assert repository.list_with_pagination(page=1, page_size=2) == first_page

    with pytest.raises(ValueError):
        repository.list_with_pagination(page=0)
    with pytest.raises(ValueError):
        repository.list_with_pagination(page_size=0)


def test_participant_repository_create_get_update_relationship_and_unique_pair(
    db_session: Session,
) -> None:
    meeting = create_meeting(db_session)
    repository = ParticipantRepository(db_session)
    participant = repository.create(
        {
            "meeting_id": meeting.id,
            "google_participant_name": "conferenceRecords/1/participants/1",
            "display_name": "Original Participant",
        }
    )

    found = repository.get_by_id(participant.id)
    assert found == participant
    assert found is not None
    assert found.meeting == meeting

    updated = repository.update(
        participant.id,
        {"display_name": "Updated Participant", "participant_type": "anonymous_user"},
    )
    assert updated is not None
    assert updated.display_name == "Updated Participant"
    assert updated.participant_type == "anonymous_user"

    assert repository.update(uuid.uuid4(), {"display_name": "Missing"}) is None

    with pytest.raises(IntegrityError):
        repository.create(
            {
                "meeting_id": meeting.id,
                "google_participant_name": "conferenceRecords/1/participants/1",
            }
        )


def test_participant_repository_rejects_missing_meeting_fk(
    db_session: Session,
) -> None:
    repository = ParticipantRepository(db_session)

    with pytest.raises(IntegrityError):
        repository.create(
            {
                "meeting_id": uuid.uuid4(),
                "google_participant_name": "conferenceRecords/missing/participants/1",
            }
        )


def test_participant_repository_upsert_uses_meeting_and_google_name_without_duplicate(
    db_session: Session,
) -> None:
    meeting = create_meeting(db_session)
    repository = ParticipantRepository(db_session)

    created = repository.upsert(
        {
            "meeting_id": meeting.id,
            "google_participant_name": "conferenceRecords/1/participants/1",
            "display_name": "Original",
        }
    )
    updated = repository.upsert(
        {
            "meeting_id": meeting.id,
            "google_participant_name": "conferenceRecords/1/participants/1",
            "display_name": "Updated",
            "email": "participant@example.com",
        }
    )

    assert updated.id == created.id
    assert updated.display_name == "Updated"
    assert updated.email == "participant@example.com"
    assert db_session.scalar(select(func.count()).select_from(Participant)) == 1


def test_participant_repository_list_with_pagination_is_deterministic(
    db_session: Session,
) -> None:
    meeting = create_meeting(db_session)
    repository = ParticipantRepository(db_session)
    participants = [
        repository.create(
            {
                "meeting_id": meeting.id,
                "google_participant_name": f"conferenceRecords/1/participants/{index}",
            }
        )
        for index in range(3)
    ]

    first_page = repository.list_with_pagination(page=1, page_size=2)
    second_page = repository.list_with_pagination(page=2, page_size=2)

    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {item.id for item in first_page + second_page} == {
        item.id for item in participants
    }
    assert repository.list_with_pagination(page=1, page_size=2) == first_page

    with pytest.raises(ValueError):
        repository.list_with_pagination(page=0)
    with pytest.raises(ValueError):
        repository.list_with_pagination(page_size=0)


def test_transcript_repository_create_get_update_relationship_and_unique_name(
    db_session: Session,
) -> None:
    meeting = create_meeting(db_session)
    repository = TranscriptRepository(db_session)
    transcript = repository.create(
        {
            "meeting_id": meeting.id,
            "google_transcript_name": "conferenceRecords/1/transcripts/1",
            "state": "STARTED",
        }
    )

    found = repository.get_by_id(transcript.id)
    assert found == transcript
    assert found is not None
    assert found.meeting == meeting

    updated = repository.update(
        transcript.id,
        {"state": "FILE_GENERATED", "processing_status": "completed"},
    )
    assert updated is not None
    assert updated.state == "FILE_GENERATED"
    assert updated.processing_status == "completed"

    assert repository.update(uuid.uuid4(), {"state": "ENDED"}) is None

    with pytest.raises(IntegrityError):
        repository.create(
            {
                "meeting_id": meeting.id,
                "google_transcript_name": "conferenceRecords/1/transcripts/1",
                "state": "FILE_GENERATED",
            }
        )


def test_transcript_repository_rejects_missing_meeting_fk(
    db_session: Session,
) -> None:
    repository = TranscriptRepository(db_session)

    with pytest.raises(IntegrityError):
        repository.create(
            {
                "meeting_id": uuid.uuid4(),
                "google_transcript_name": "conferenceRecords/missing/transcripts/1",
                "state": "FILE_GENERATED",
            }
        )


def test_transcript_repository_upsert_uses_google_transcript_name_without_duplicate(
    db_session: Session,
) -> None:
    meeting = create_meeting(db_session)
    repository = TranscriptRepository(db_session)

    created = repository.upsert(
        {
            "meeting_id": meeting.id,
            "google_transcript_name": "conferenceRecords/1/transcripts/1",
            "state": "STARTED",
        }
    )
    updated = repository.upsert(
        {
            "meeting_id": meeting.id,
            "google_transcript_name": "conferenceRecords/1/transcripts/1",
            "state": "FILE_GENERATED",
            "processing_status": "completed",
        }
    )

    assert updated.id == created.id
    assert updated.state == "FILE_GENERATED"
    assert updated.processing_status == "completed"
    assert db_session.scalar(select(func.count()).select_from(Transcript)) == 1


def test_transcript_repository_list_with_pagination_is_deterministic(
    db_session: Session,
) -> None:
    meeting = create_meeting(db_session)
    repository = TranscriptRepository(db_session)
    transcripts = [
        repository.create(
            {
                "meeting_id": meeting.id,
                "google_transcript_name": f"conferenceRecords/1/transcripts/{index}",
                "state": "FILE_GENERATED",
            }
        )
        for index in range(3)
    ]

    first_page = repository.list_with_pagination(page=1, page_size=2)
    second_page = repository.list_with_pagination(page=2, page_size=2)

    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {item.id for item in first_page + second_page} == {
        item.id for item in transcripts
    }
    assert repository.list_with_pagination(page=1, page_size=2) == first_page

    with pytest.raises(ValueError):
        repository.list_with_pagination(page=0)
    with pytest.raises(ValueError):
        repository.list_with_pagination(page_size=0)


def test_transcript_entry_repository_create_get_update_relationships_and_unique_name(
    db_session: Session,
) -> None:
    meeting = create_meeting(db_session)
    participant = ParticipantRepository(db_session).create(
        {
            "meeting_id": meeting.id,
            "google_participant_name": "conferenceRecords/1/participants/1",
        }
    )
    transcript = create_transcript(db_session, meeting)
    repository = TranscriptEntryRepository(db_session)
    entry = repository.create(
        {
            "transcript_id": transcript.id,
            "participant_id": participant.id,
            "google_entry_name": "conferenceRecords/1/transcripts/1/entries/1",
            "text": "Original text.",
        }
    )

    found = repository.get_by_id(entry.id)
    assert found == entry
    assert found is not None
    assert found.transcript == transcript
    assert found.participant == participant

    updated = repository.update(
        entry.id,
        {"text": "Updated text.", "language_code": "en-US"},
    )
    assert updated is not None
    assert updated.text == "Updated text."
    assert updated.language_code == "en-US"

    assert repository.update(uuid.uuid4(), {"text": "Missing"}) is None

    with pytest.raises(IntegrityError):
        repository.create(
            {
                "transcript_id": transcript.id,
                "google_entry_name": "conferenceRecords/1/transcripts/1/entries/1",
                "text": "Duplicate text.",
            }
        )


def test_transcript_entry_repository_supports_null_participant_id(
    db_session: Session,
) -> None:
    meeting = create_meeting(db_session)
    transcript = create_transcript(db_session, meeting)
    repository = TranscriptEntryRepository(db_session)

    entry = repository.create(
        {
            "transcript_id": transcript.id,
            "participant_id": None,
            "google_entry_name": "conferenceRecords/1/transcripts/1/entries/no-speaker",
            "text": "Unknown speaker text.",
        }
    )

    assert entry.participant_id is None
    assert entry.participant is None
    assert entry.transcript == transcript


def test_transcript_entry_repository_rejects_missing_fk(
    db_session: Session,
) -> None:
    repository = TranscriptEntryRepository(db_session)

    with pytest.raises(IntegrityError):
        repository.create(
            {
                "transcript_id": uuid.uuid4(),
                "google_entry_name": "conferenceRecords/missing/transcripts/1/entries/1",
                "text": "Missing transcript.",
            }
        )


def test_transcript_entry_repository_upsert_uses_google_entry_name_without_duplicate(
    db_session: Session,
) -> None:
    meeting = create_meeting(db_session)
    transcript = create_transcript(db_session, meeting)
    repository = TranscriptEntryRepository(db_session)

    created = repository.upsert(
        {
            "transcript_id": transcript.id,
            "google_entry_name": "conferenceRecords/1/transcripts/1/entries/1",
            "text": "Original text.",
        }
    )
    updated = repository.upsert(
        {
            "transcript_id": transcript.id,
            "google_entry_name": "conferenceRecords/1/transcripts/1/entries/1",
            "text": "Updated text.",
            "language_code": "en-US",
        }
    )

    assert updated.id == created.id
    assert updated.text == "Updated text."
    assert updated.language_code == "en-US"
    assert db_session.scalar(select(func.count()).select_from(TranscriptEntry)) == 1


def test_transcript_entry_repository_list_with_pagination_is_deterministic(
    db_session: Session,
) -> None:
    meeting = create_meeting(db_session)
    transcript = create_transcript(db_session, meeting)
    repository = TranscriptEntryRepository(db_session)
    entries = [
        repository.create(
            {
                "transcript_id": transcript.id,
                "google_entry_name": f"conferenceRecords/1/transcripts/1/entries/{index}",
                "text": f"Entry {index}.",
            }
        )
        for index in range(3)
    ]

    first_page = repository.list_with_pagination(page=1, page_size=2)
    second_page = repository.list_with_pagination(page=2, page_size=2)

    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {item.id for item in first_page + second_page} == {item.id for item in entries}
    assert repository.list_with_pagination(page=1, page_size=2) == first_page

    with pytest.raises(ValueError):
        repository.list_with_pagination(page=0)
    with pytest.raises(ValueError):
        repository.list_with_pagination(page_size=0)
