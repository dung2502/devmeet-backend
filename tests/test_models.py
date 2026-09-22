from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Meeting, Participant, Transcript, TranscriptEntry, User


def commit_and_refresh(db: Session, instance: object) -> object:
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def make_user(db: Session, suffix: str = "1") -> User:
    user = User(
        google_user_id=f"google-user-{suffix}",
        email=f"user-{suffix}@example.com",
        display_name=f"User {suffix}",
    )
    return commit_and_refresh(db, user)


def make_meeting(db: Session, user: User, suffix: str = "1") -> Meeting:
    meeting = Meeting(
        user_id=user.id,
        conference_record_name=f"conferenceRecords/{suffix}",
        meeting_space_name=f"spaces/{suffix}",
        meeting_url=f"https://meet.google.com/{suffix}",
        title=f"Meeting {suffix}",
        start_time=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
    )
    return commit_and_refresh(db, meeting)


def make_participant(db: Session, meeting: Meeting, suffix: str = "1") -> Participant:
    participant = Participant(
        meeting_id=meeting.id,
        google_participant_name=f"conferenceRecords/1/participants/{suffix}",
        display_name=f"Participant {suffix}",
        email=f"participant-{suffix}@example.com",
    )
    return commit_and_refresh(db, participant)


def make_transcript(db: Session, meeting: Meeting, suffix: str = "1") -> Transcript:
    transcript = Transcript(
        meeting_id=meeting.id,
        google_transcript_name=f"conferenceRecords/1/transcripts/{suffix}",
        state="FILE_GENERATED",
    )
    return commit_and_refresh(db, transcript)


def test_user_create_read_required_fields_and_timestamps(db_session: Session) -> None:
    user = make_user(db_session)

    found = db_session.get(User, user.id)

    assert found is not None
    assert found.google_user_id == "google-user-1"
    assert found.email == "user-1@example.com"
    assert found.created_at.tzinfo is not None
    assert found.updated_at.tzinfo is not None

    db_session.add(User(email="missing-google@example.com"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_user_unique_google_user_id(db_session: Session) -> None:
    make_user(db_session, "1")

    db_session.add(
        User(
            google_user_id="google-user-1",
            email="another@example.com",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_user_unique_email(db_session: Session) -> None:
    make_user(db_session, "1")

    db_session.add(
        User(
            google_user_id="another-google-user",
            email="user-1@example.com",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_meeting_create_read_relationship_defaults_unique_and_jsonb(
    db_session: Session,
) -> None:
    user = make_user(db_session)
    ai_result = {
        "summary": "Sprint planning summary",
        "key_points": ["API ready"],
        "decisions": [
            {
                "decision": "Release Friday",
                "context": "After integration test",
                "evidence_timestamp": "09:15:10",
            }
        ],
        "action_items": [
            {
                "task": "Finish API docs",
                "assignee": "User 1",
                "deadline": "2026-08-22",
                "status": "TODO",
                "evidence_timestamp": "09:20:05",
            }
        ],
        "follow_up_email": {"subject": "Summary", "body": "Body"},
    }
    meeting = Meeting(
        user_id=user.id,
        conference_record_name="conferenceRecords/meeting-jsonb",
        ai_result=ai_result,
    )
    commit_and_refresh(db_session, meeting)

    found = db_session.get(Meeting, meeting.id)

    assert found is not None
    assert found.user == user
    assert user.meetings == [found]
    assert found.ai_result == ai_result
    assert found.ai_status == "NOT_PROCESSED"
    assert found.sheets_sync_status == "NOT_SYNCED"
    assert found.dom_capture_status == "not_captured"
    assert found.comparison_status == "pending"

    db_session.add(
        Meeting(
            user_id=user.id,
            conference_record_name="conferenceRecords/meeting-jsonb",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_participant_create_read_fk_relationship_and_unique_pair(
    db_session: Session,
) -> None:
    user = make_user(db_session)
    meeting = make_meeting(db_session, user)
    participant = make_participant(db_session, meeting)

    found = db_session.get(Participant, participant.id)

    assert found is not None
    assert found.meeting == meeting
    assert meeting.participants == [found]
    assert found.participant_type == "signed_in_user"

    db_session.add(
        Participant(
            meeting_id=meeting.id,
            google_participant_name=participant.google_participant_name,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_transcript_create_read_fk_relationship_and_unique_name(
    db_session: Session,
) -> None:
    user = make_user(db_session)
    meeting = make_meeting(db_session, user)
    transcript = make_transcript(db_session, meeting)

    found = db_session.get(Transcript, transcript.id)

    assert found is not None
    assert found.meeting == meeting
    assert meeting.transcripts == [found]
    assert found.processing_status == "pending"

    db_session.add(
        Transcript(
            meeting_id=meeting.id,
            google_transcript_name=transcript.google_transcript_name,
            state="FILE_GENERATED",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_transcript_entry_create_read_relationships_optional_participant_and_unique_name(
    db_session: Session,
) -> None:
    user = make_user(db_session)
    meeting = make_meeting(db_session, user)
    participant = make_participant(db_session, meeting)
    transcript = make_transcript(db_session, meeting)
    entry = TranscriptEntry(
        transcript_id=transcript.id,
        participant_id=participant.id,
        google_entry_name="conferenceRecords/1/transcripts/1/entries/1",
        text="We will release on Friday.",
        language_code="en-US",
        start_time=datetime(2026, 8, 21, 9, 15, 10, tzinfo=UTC),
        end_time=datetime(2026, 8, 21, 9, 15, 15, tzinfo=UTC),
    )
    commit_and_refresh(db_session, entry)

    found = db_session.get(TranscriptEntry, entry.id)

    assert found is not None
    assert found.transcript == transcript
    assert found.participant == participant
    assert transcript.entries == [found]
    assert participant.transcript_entries == [found]
    assert found.source == "google_meet"
    assert found.start_time is not None
    assert found.start_time.tzinfo is not None

    no_participant_entry = TranscriptEntry(
        transcript_id=transcript.id,
        participant_id=None,
        google_entry_name="conferenceRecords/1/transcripts/1/entries/2",
        text="Unknown speaker text.",
    )
    commit_and_refresh(db_session, no_participant_entry)
    assert no_participant_entry.participant is None

    db_session.add(
        TranscriptEntry(
            transcript_id=transcript.id,
            google_entry_name=entry.google_entry_name,
            text="Duplicate entry.",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()

