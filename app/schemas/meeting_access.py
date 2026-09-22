import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MeetingAccessBase(BaseModel):
    meeting_id: uuid.UUID
    user_id: uuid.UUID
    role: str = "PARTICIPANT"


class MeetingAccessCreate(MeetingAccessBase):
    pass


class MeetingAccessUpdate(BaseModel):
    role: str | None = None
    last_seen_at: datetime | None = None


class MeetingAccessResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    meeting_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    first_joined_at: datetime
    last_seen_at: datetime
