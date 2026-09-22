import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models.live_session import LiveSessionStatus


class LiveSessionCreateRequest(BaseModel):
    meeting_id: uuid.UUID
    tab_session_uuid: uuid.UUID


class LiveSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: uuid.UUID
    meeting_id: uuid.UUID
    user_id: uuid.UUID
    tab_session_uuid: uuid.UUID
    status: str
    created_at: datetime
    updated_at: datetime


class FinalizedSegment(BaseModel):
    """A single finalized DOM caption segment ready for backend persistence."""

    segment_id: str
    speaker_name: str | None = None
    text: str
    sequence: int
    source: Literal["DOM"] = "DOM"
    start_time_offset_ms: int | None = None
    end_time_offset_ms: int | None = None
    observed_start_epoch_ms: int | None = None
    observed_end_epoch_ms: int | None = None
    confidence_score: float = 1.0


class FinalizeTranscriptRequest(BaseModel):
    """Payload sent by Extension to POST /live-sessions/{session_id}/finalize."""

    meeting_id: uuid.UUID
    segments: list[FinalizedSegment]
    segment_count: int
    captured_at: str  # ISO 8601 timestamp string


class FinalizeTranscriptResponse(BaseModel):
    """Response from POST /live-sessions/{session_id}/finalize."""

    session_id: uuid.UUID
    meeting_id: uuid.UUID
    dom_capture_status: str
    status: str  # "accepted"
    raw_segments_ingested: int = 0


# ─── Heartbeat ───────────────────────────────────────────────────────────────

class LiveSessionHeartbeatRequest(BaseModel):
    """Payload for POST /live-sessions/{session_id}/heartbeat."""

    client_timestamp_ms: int


class LiveSessionHeartbeatResponse(BaseModel):
    """Response from POST /live-sessions/{session_id}/heartbeat."""

    model_config = ConfigDict(from_attributes=True)

    session_id: uuid.UUID
    meeting_id: uuid.UUID
    status: str
    server_timestamp_ms: int
    clock_offset_ms: int
    active_capture_sessions: int
    last_heartbeat_at: datetime | None = None


