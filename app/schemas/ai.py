import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MeetingAIProcessRequest(BaseModel):
    request_id: str | None = Field(
        default=None,
        description="Optional client-provided idempotency request ID",
    )
    tasks: list[str] | None = Field(
        default=None,
        description="Optional list of AI tasks to run (defaults to all standard tasks)",
    )
    force_reprocess: bool = Field(
        default=False,
        description="If True, bypasses cached AI output and forces a complete re-run of AI_PROCESS",
    )


class DecisionItem(BaseModel):
    decision: str
    context: str | None = None
    evidence_timestamp: str | None = None
    owner: str | None = None


class ActionItem(BaseModel):
    task: str
    assignee: str | None = None
    deadline: str | None = None
    due_date: str | None = None
    priority: str | None = None
    status: str = "TODO"
    evidence_timestamp: str | None = None


class FollowUpEmail(BaseModel):
    subject: str = ""
    body: str = ""


class AIOutput(BaseModel):
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    action_items: list[dict[str, Any]] = Field(default_factory=list)
    follow_up_email: dict[str, Any] = Field(default_factory=dict)


class ObservabilityInfo(BaseModel):
    provider: str = "google"
    model: str = "gemini-3.6-flash"
    execution_time_ms: int = 0


class MeetingAIProcessResponse(BaseModel):
    status: str
    request_id: str
    meeting_id: uuid.UUID
    google_sheets_url: str | None = None
    ai_status: str
    sheets_sync_status: str
    sheets_error_warning: Any | None = None
    observability: dict[str, Any] | None = None
    ai_output: dict[str, Any] | None = None


class MeetingAIStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    meeting_id: uuid.UUID
    ai_status: str
    sheets_sync_status: str
    has_cached_result: bool
    processed_at: datetime | None = None
