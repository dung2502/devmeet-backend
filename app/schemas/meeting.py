import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class MeetingSyncRequest(BaseModel):
    conference_record_name: str | None = None
    meeting_id: uuid.UUID | None = None
    meeting_space_name: str | None = None
    meeting_url: str | None = None
    title: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    user_id: uuid.UUID | None = None


class MeetingUpdateRequest(BaseModel):
    title: str


class MeetingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    conference_record_name: str | None = None
    meeting_space_name: str | None = None
    meeting_url: str | None = None
    title: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    status: str
    transcript_status: str
    ai_status: str
    sheets_sync_status: str
    dom_capture_status: str
    created_at: datetime
    updated_at: datetime


class MeetingListItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    conference_record_name: str | None = None
    meeting_space_name: str | None = None
    meeting_url: str | None = None
    meeting_code: str | None = None
    title: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    status: str
    transcript_status: str
    dom_capture_status: str
    ai_status: str
    sheets_sync_status: str
    selected_transcript_source: str | None = None
    participant_count: int = 0
    transcript_entry_count: int = 0
    user_role: str = "OWNER"
    host_name: str | None = None
    created_at: datetime
    updated_at: datetime


class MeetingListResponse(BaseModel):
    items: list[MeetingListItemResponse]
    page: int
    page_size: int
    total: int


class MeetingDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    conference_record_name: str | None = None
    meeting_space_name: str | None = None
    meeting_url: str | None = None
    meeting_code: str | None = None
    title: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    status: str
    transcript_status: str
    dom_capture_status: str
    ai_status: str
    sheets_sync_status: str
    google_sheets_url: str | None = None
    ai_result: dict[str, Any] | None = None
    comparison_status: str | None = None
    comparison_metrics: dict[str, Any] | None = None
    selected_transcript_source: str | None = None
    participants: list[dict[str, Any]] = []
    transcript_entry_count: int = 0
    user_role: str = "OWNER"
    host_name: str | None = None
    created_at: datetime
    updated_at: datetime


# ─── Phase 5D: Meeting Ended ─────────────────────────────────────────────────

class MeetingEndedResponse(BaseModel):
    """Response from POST /meetings/{meeting_id}/ended."""

    id: uuid.UUID
    status: str
    transcript_status: str
    message: str

