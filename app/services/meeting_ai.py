import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.n8n import (
    N8nClient,
    N8nClientError,
    N8nConnectionError,
    N8nExecutionError,
    N8nMalformedResponseError,
    N8nTimeoutError,
)
from app.repositories.meeting_repository import MeetingRepository
from app.schemas.ai import MeetingAIProcessResponse, MeetingAIStatusResponse
from app.services.participant_sync import MeetingNotFoundError
from app.services.transcript_sync import TranscriptNotReadyError
from app.services.transcript_token_optimizer import (
    TranscriptInputTooLargeError,
    TranscriptTokenOptimizer,
)

logger = logging.getLogger(__name__)


class MeetingAIProcessingConflictError(Exception):
    """Raised when a concurrent request attempts to process a meeting currently in PROCESSING state."""

    def __init__(self, message: str = "Meeting is currently being processed by AI.") -> None:
        self.message = message
        self.status_code = 409
        self.error_code = "MEETING_AI_PROCESSING"
        super().__init__(message)


class MeetingAIService:
    """Service to orchestrate Meeting AI processing, Phase 3 token optimization, and n8n webhook calls."""

    def __init__(
        self,
        session: Session,
        n8n_client: N8nClient | None = None,
    ) -> None:
        self.session = session
        self.meeting_repo = MeetingRepository(session)
        self.n8n_client = n8n_client or N8nClient()

    def process_meeting_ai(
        self,
        meeting_id: uuid.UUID,
        request_id: str | None = None,
        tasks: list[str] | None = None,
        force_reprocess: bool = False,
    ) -> MeetingAIProcessResponse:
        settings = get_settings()
        meeting = self.meeting_repo.get_by_id(meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

        # Concurrency & Idempotency safety: PROCESSING check (with 5-minute stale recovery)
        if meeting.ai_status == "PROCESSING":
            now = datetime.now(timezone.utc)
            updated_at = meeting.updated_at or meeting.created_at
            if updated_at and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            is_stale = bool(updated_at and (now - updated_at).total_seconds() > 300)

            if not is_stale:
                raise MeetingAIProcessingConflictError(
                    f"Meeting {meeting_id} is currently being processed by AI."
                )

        # Establish deterministic / stable logical request_id
        if request_id and request_id.strip():
            req_id = request_id.strip()
        elif force_reprocess:
            req_id = f"req-{uuid.uuid4()}"
        else:
            req_id = f"req-{meeting.id}"

        # -------------------------------------------------------------------
        # CASE A: Cache Hit (Fully Processed & Synced)
        # -------------------------------------------------------------------
        if (
            not force_reprocess
            and meeting.ai_status == "COMPLETED"
            and meeting.ai_result is not None
            and meeting.sheets_sync_status == "SYNCED"
        ):
            logger.info("Cache hit for meeting %s (COMPLETED, SYNCED). Returning DB result.", meeting_id)
            sheets_url = (
                f"https://docs.google.com/spreadsheets/d/{settings.devmeet_spreadsheet_id}"
                if settings.devmeet_spreadsheet_id
                else None
            )
            return MeetingAIProcessResponse(
                status="success",
                request_id=req_id,
                meeting_id=meeting.id,
                google_sheets_url=sheets_url,
                ai_status="COMPLETED",
                sheets_sync_status="SYNCED",
                sheets_error_warning=None,
                observability={
                    "provider": "google",
                    "model": "gemini-3.6-flash",
                    "execution_time_ms": 0,
                    "cache_hit": True,
                },
                ai_output=meeting.ai_result,
            )

        # -------------------------------------------------------------------
        # CASE B: SHEETS_RETRY (AI already COMPLETED, Sheets FAILED)
        # -------------------------------------------------------------------
        if (
            not force_reprocess
            and meeting.ai_status == "COMPLETED"
            and meeting.ai_result is not None
            and meeting.sheets_sync_status == "FAILED"
        ):
            logger.info("Executing SHEETS_RETRY for meeting %s using cached ai_result.", meeting_id)
            meeting_date = (
                meeting.start_time.strftime("%Y-%m-%d")
                if meeting.start_time
                else datetime.now(timezone.utc).strftime("%Y-%m-%d")
            )
            retry_payload: dict[str, Any] = {
                "action": "SHEETS_RETRY",
                "request_id": req_id,
                "meeting_id": str(meeting.id),
                "meeting_title": meeting.title or "Meeting",
                "meeting_date": meeting_date,
                "tasks": tasks or ["summary", "decisions", "action_items", "follow_up_email"],
                "cached_ai_result": meeting.ai_result,
            }

            start_t = time.perf_counter()
            n8n_resp = self.n8n_client.trigger_workflow(retry_payload)
            exec_time_ms = int((time.perf_counter() - start_t) * 1000)

            sheets_status = n8n_resp.get("sheets_sync_status", "FAILED")
            warnings = n8n_resp.get("warnings")

            if sheets_status == "SYNCED":
                self.meeting_repo.update(meeting.id, {"sheets_sync_status": "SYNCED"})
                final_status = "success"
            else:
                self.meeting_repo.update(meeting.id, {"sheets_sync_status": "FAILED"})
                final_status = "partial_success"

            sheets_url = (
                f"https://docs.google.com/spreadsheets/d/{settings.devmeet_spreadsheet_id}"
                if settings.devmeet_spreadsheet_id
                else None
            )

            return MeetingAIProcessResponse(
                status=final_status,
                request_id=req_id,
                meeting_id=meeting.id,
                google_sheets_url=sheets_url,
                ai_status="COMPLETED",
                sheets_sync_status=sheets_status,
                sheets_error_warning=warnings if final_status == "partial_success" else None,
                observability={
                    "provider": "google",
                    "model": "gemini-3.6-flash",
                    "execution_time_ms": exec_time_ms,
                    "action": "SHEETS_RETRY",
                },
                ai_output=meeting.ai_result,
            )

        # -------------------------------------------------------------------
        # CASE C, D, E: Full AI_PROCESS
        # -------------------------------------------------------------------
        # 1. Phase 3 Source Selection & Token Optimization
        optimizer = TranscriptTokenOptimizer(self.session)
        opt_result = optimizer.optimize_by_meeting_id(meeting_id, raise_on_limit=True)

        if opt_result.selected_source == "none" or not opt_result.optimized_text.strip():
            raise TranscriptNotReadyError(
                message="Transcript is not ready or unavailable for this meeting.",
                state="not_available",
            )

        # 2. State transition: PROCESSING (persisted in DB before external call)
        self.meeting_repo.update(meeting.id, {"ai_status": "PROCESSING"})

        # 3. Format n8n payload
        meeting_date = (
            meeting.start_time.strftime("%Y-%m-%d")
            if meeting.start_time
            else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        participants_data = [
            {
                "id": str(p.id),
                "name": p.display_name or p.email or "Participant",
                "email": p.email or "",
            }
            for p in (meeting.participants or [])
        ]

        ai_payload: dict[str, Any] = {
            "action": "AI_PROCESS",
            "request_id": req_id,
            "meeting_id": str(meeting.id),
            "meeting_title": meeting.title or "Meeting",
            "meeting_date": meeting_date,
            "transcript_prompt_text": opt_result.optimized_text,
            "selected_source": opt_result.selected_source,
            "tasks": tasks or ["summary", "decisions", "action_items", "follow_up_email"],
            "participants": participants_data,
            "force_reprocess": force_reprocess,
        }

        start_t = time.perf_counter()
        try:
            n8n_resp = self.n8n_client.trigger_workflow(ai_payload)
        except (N8nTimeoutError, N8nConnectionError) as exc:
            # Conservative recovery: keep PROCESSING state to prevent duplicate external execution on unconfirmed completion
            logger.error(
                "n8n AI execution encountered gateway timeout/connection error for meeting %s (request_id=%s): %s. "
                "Preserving PROCESSING state to guard against duplicate AI runs.",
                meeting_id,
                req_id,
                exc,
            )
            raise
        except Exception as exc:
            logger.error("n8n execution failed for meeting %s: %s", meeting_id, exc)
            self.meeting_repo.update(
                meeting.id,
                {"ai_status": "FAILED", "sheets_sync_status": "NOT_SYNCED"},
            )
            raise

        exec_time_ms = int((time.perf_counter() - start_t) * 1000)

        # 4. Response Validation
        raw_ai_output = n8n_resp.get("result") or n8n_resp.get("ai_output")
        if not isinstance(raw_ai_output, dict) or not raw_ai_output.get("summary"):
            logger.error("Invalid AI structured output received from n8n for meeting %s", meeting_id)
            self.meeting_repo.update(
                meeting.id,
                {"ai_status": "FAILED", "sheets_sync_status": "NOT_SYNCED"},
            )
            raise N8nExecutionError("n8n returned malformed or incomplete AI output.")

        returned_ai_status = n8n_resp.get("ai_status", "COMPLETED")
        returned_sheets_status = n8n_resp.get("sheets_sync_status", "NOT_SYNCED")
        warnings = n8n_resp.get("warnings")

        if returned_ai_status != "COMPLETED":
            self.meeting_repo.update(
                meeting.id,
                {"ai_status": "FAILED", "sheets_sync_status": "NOT_SYNCED"},
            )
            raise N8nExecutionError(f"n8n AI processing failed: {n8n_resp.get('error') or 'Unknown error'}")

        # 5. Persist Final State
        if returned_sheets_status == "SYNCED":
            self.meeting_repo.update(
                meeting.id,
                {
                    "ai_status": "COMPLETED",
                    "ai_result": raw_ai_output,
                    "sheets_sync_status": "SYNCED",
                },
            )
            final_status = "success"
        else:
            self.meeting_repo.update(
                meeting.id,
                {
                    "ai_status": "COMPLETED",
                    "ai_result": raw_ai_output,
                    "sheets_sync_status": "FAILED",
                },
            )
            final_status = "partial_success"

        sheets_url = (
            f"https://docs.google.com/spreadsheets/d/{settings.devmeet_spreadsheet_id}"
            if settings.devmeet_spreadsheet_id
            else None
        )

        return MeetingAIProcessResponse(
            status=final_status,
            request_id=req_id,
            meeting_id=meeting.id,
            google_sheets_url=sheets_url,
            ai_status="COMPLETED",
            sheets_sync_status=returned_sheets_status,
            sheets_error_warning=warnings if final_status == "partial_success" else None,
            observability={
                "provider": "google",
                "model": "gemini-3.6-flash",
                "execution_time_ms": exec_time_ms,
                "selected_source": opt_result.selected_source,
            },
            ai_output=raw_ai_output,
        )

    def get_meeting_ai_status(self, meeting_id: uuid.UUID) -> MeetingAIStatusResponse:
        meeting = self.meeting_repo.get_by_id(meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

        return MeetingAIStatusResponse(
            meeting_id=meeting.id,
            ai_status=meeting.ai_status,
            sheets_sync_status=meeting.sheets_sync_status,
            has_cached_result=meeting.ai_result is not None,
            processed_at=meeting.updated_at if meeting.ai_status == "COMPLETED" else None,
        )
