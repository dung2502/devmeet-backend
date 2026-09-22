import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.integrations import GoogleMeetAuthenticationError, GoogleMeetClient
from app.models import Participant
from app.repositories import MeetingRepository, ParticipantRepository
from app.schemas.participant import ParticipantResponse, ParticipantSyncResponse
from app.services.participant_sync import MeetingNotFoundError, ParticipantSyncService

router = APIRouter(prefix="/meetings", tags=["participants"])


@router.post(
    "/{meeting_id}/participants/sync",
    response_model=ParticipantSyncResponse,
    status_code=status.HTTP_200_OK,
)
def sync_participants(
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
            "X-Google-Access-Token header or access_token query param is required for participant sync."
        )

    part_repo = ParticipantRepository(db)
    client = GoogleMeetClient(access_token=token)
    service = ParticipantSyncService(part_repo, client, meeting_repo)

    items = service.sync_participants(meeting.conference_record_name, meeting_id)
    return ParticipantSyncResponse(
        meeting_id=meeting_id,
        participants_synced=len(items),
        items=items,
    )


@router.get(
    "/{meeting_id}/participants",
    response_model=list[ParticipantResponse],
    status_code=status.HTTP_200_OK,
)
def get_participants(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Any:
    meeting_repo = MeetingRepository(db)
    meeting = meeting_repo.get_by_id(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

    participants = list(
        db.scalars(
            select(Participant)
            .where(Participant.meeting_id == meeting_id)
            .order_by(Participant.created_at)
        )
    )
    return participants