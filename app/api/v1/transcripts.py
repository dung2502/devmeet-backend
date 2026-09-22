import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.integrations import GoogleMeetAuthenticationError, GoogleMeetClient
from app.models import Participant, Transcript, TranscriptEntry
from app.repositories import (
    MeetingRepository,
    ParticipantRepository,
    TranscriptEntryRepository,
    TranscriptRepository,
)
from app.schemas.transcript import (
    TranscriptEntriesListResponse,
    TranscriptEntryResponse,
    TranscriptResponse,
    TranscriptStatusResponse,
    TranscriptSyncResponse,
)
from app.services.participant_sync import MeetingNotFoundError
from app.services.transcript_sync import TranscriptSyncService

router = APIRouter(prefix="/meetings", tags=["transcripts"])


@router.post(
    "/{meeting_id}/transcript/sync",
    response_model=TranscriptSyncResponse,
    status_code=status.HTTP_200_OK,
)
def sync_transcript(
    meeting_id: uuid.UUID,
    x_google_access_token: str | None = Header(None, alias="X-Google-Access-Token"),
    access_token: str | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    meeting_repo = MeetingRepository(db)
    meeting = meeting_repo.get_by_id(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

    token = x_google_access_token or access_token
    if not token:
        raise GoogleMeetAuthenticationError(
            "X-Google-Access-Token header or access_token query param is required for transcript sync."
        )

    tx_repo = TranscriptRepository(db)
    entry_repo = TranscriptEntryRepository(db)
    part_repo = ParticipantRepository(db)
    client = GoogleMeetClient(access_token=token)

    service = TranscriptSyncService(
        tx_repo, entry_repo, part_repo, client, meeting_repo, db
    )

    synced_transcripts = service.sync_transcripts(
        meeting.conference_record_name, meeting_id
    )

    if not synced_transcripts:
        return TranscriptSyncResponse(
            meeting_id=meeting_id,
            transcript_id=None,
            status="not_available",
            entries_synced=0,
        )

    tx = synced_transcripts[0]

    # Count entries synced for this transcript
    entries_count = db.scalar(
        select(func.count(TranscriptEntry.id)).where(
            TranscriptEntry.transcript_id == tx.id
        )
    ) or 0

    # Update meeting transcript_status if entries were synced
    if entries_count > 0:
        meeting_repo.update(meeting_id, {"transcript_status": "available"})

    return TranscriptSyncResponse(
        meeting_id=meeting_id,
        transcript_id=tx.id,
        status="available" if entries_count > 0 else tx.state.lower(),
        entries_synced=entries_count,
    )


@router.get(
    "/{meeting_id}/transcript",
    response_model=TranscriptResponse,
    status_code=status.HTTP_200_OK,
)
def get_transcript(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Any:
    meeting_repo = MeetingRepository(db)
    meeting = meeting_repo.get_by_id(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

    tx = db.scalar(
        select(Transcript)
        .where(Transcript.meeting_id == meeting_id)
        .order_by(Transcript.created_at.desc())
    )
    if tx is None:
        raise MeetingNotFoundError(f"No transcript found for meeting {meeting_id}.")

    return tx


@router.get(
    "/{meeting_id}/transcript/status",
    response_model=TranscriptStatusResponse,
    status_code=status.HTTP_200_OK,
)
def get_transcript_status(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Any:
    meeting_repo = MeetingRepository(db)
    meeting = meeting_repo.get_by_id(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

    tx = db.scalar(
        select(Transcript)
        .where(Transcript.meeting_id == meeting_id)
        .order_by(Transcript.created_at.desc())
    )

    entries_count = (
        db.scalar(
            select(func.count(TranscriptEntry.id))
            .join(Transcript)
            .where(Transcript.meeting_id == meeting_id)
        )
        or 0
    )

    return TranscriptStatusResponse(
        meeting_id=meeting_id,
        transcript_status=meeting.transcript_status,
        entries_count=entries_count,
        fetched_at=tx.fetched_at if tx else None,
    )


@router.get(
    "/{meeting_id}/transcript/entries",
    response_model=TranscriptEntriesListResponse,
    status_code=status.HTTP_200_OK,
)
def get_transcript_entries(
    meeting_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    participant_id: uuid.UUID | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    meeting_repo = MeetingRepository(db)
    meeting = meeting_repo.get_by_id(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

    base_query = (
        select(TranscriptEntry)
        .join(Transcript)
        .where(Transcript.meeting_id == meeting_id)
    )
    if participant_id is not None:
        base_query = base_query.where(TranscriptEntry.participant_id == participant_id)

    total = db.scalar(select(func.count()).select_from(base_query.subquery())) or 0
    offset = (page - 1) * page_size

    entries = list(
        db.scalars(
            base_query.order_by(TranscriptEntry.created_at, TranscriptEntry.id)
            .offset(offset)
            .limit(page_size)
        )
    )

    items: list[dict[str, Any]] = []
    part_map = {
        p.id: p.display_name
        for p in db.scalars(
            select(Participant).where(Participant.meeting_id == meeting_id)
        )
    }

    for e in entries:
        item_dict = {
            "id": e.id,
            "transcript_id": e.transcript_id,
            "participant_id": e.participant_id,
            "speaker": part_map.get(e.participant_id) if e.participant_id else None,
            "google_entry_name": e.google_entry_name,
            "text": e.text,
            "language_code": e.language_code,
            "start_time": e.start_time,
            "end_time": e.end_time,
            "source": e.source,
            "created_at": e.created_at,
        }
        items.append(item_dict)

    return TranscriptEntriesListResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
    )