import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict

SourceTypeEnum = Literal["OFFICIAL", "DOM", "NONE"]


class TranscriptViewEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    speaker: str
    timestamp: str
    text: str


class TranscriptViewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    meeting_id: uuid.UUID
    source: SourceTypeEnum
    total_entries: int
    reason: str
    entries: list[TranscriptViewEntry]
    cross_session_coverage: float | None = None
    session_contributors: list[str] | None = None
    active_capture_sessions: int | None = None

