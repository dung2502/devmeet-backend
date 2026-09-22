import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MeetingDOMSegmentCreate(BaseModel):
    segment_id: uuid.UUID | None = None
    meeting_id: uuid.UUID
    session_id: uuid.UUID
    sequence: int
    speaker_name: str
    text: str
    start_time_offset_ms: int | None = None
    end_time_offset_ms: int | None = None
    observed_start_epoch_ms: int
    observed_end_epoch_ms: int
    is_final: bool = True
    confidence_score: float = 1.0


class MeetingDOMSegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    segment_id: uuid.UUID
    meeting_id: uuid.UUID
    session_id: uuid.UUID
    sequence: int
    speaker_name: str
    text: str
    start_time_offset_ms: int | None = None
    end_time_offset_ms: int | None = None
    observed_start_epoch_ms: int
    observed_end_epoch_ms: int
    is_final: bool
    confidence_score: float
    created_at: datetime
