import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TranscriptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    meeting_id: uuid.UUID
    google_transcript_name: str
    state: str
    docs_url: str | None = None
    processing_status: str
    fetched_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TranscriptSyncResponse(BaseModel):
    meeting_id: uuid.UUID
    transcript_id: uuid.UUID | None = None
    status: str
    entries_synced: int


class TranscriptStatusResponse(BaseModel):
    meeting_id: uuid.UUID
    transcript_status: str
    entries_count: int
    fetched_at: datetime | None = None


class TranscriptEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    transcript_id: uuid.UUID
    participant_id: uuid.UUID | None = None
    speaker: str | None = None
    google_entry_name: str
    text: str
    language_code: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    source: str
    created_at: datetime


class TranscriptEntriesListResponse(BaseModel):
    items: list[TranscriptEntryResponse]
    page: int
    page_size: int
    total: int
