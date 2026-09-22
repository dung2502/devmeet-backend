import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ParticipantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    meeting_id: uuid.UUID
    google_participant_name: str
    display_name: str | None = None
    email: str | None = None
    participant_type: str
    join_time: datetime | None = None
    leave_time: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ParticipantSyncResponse(BaseModel):
    meeting_id: uuid.UUID
    participants_synced: int
    items: list[ParticipantResponse]
