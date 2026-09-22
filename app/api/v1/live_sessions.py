import uuid
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.live_session import (
    FinalizeTranscriptRequest,
    FinalizeTranscriptResponse,
    LiveSessionCreateRequest,
    LiveSessionHeartbeatRequest,
    LiveSessionHeartbeatResponse,
    LiveSessionResponse,
)
from app.services.finalize_service import FinalizeService
from app.services.live_session_service import LiveSessionService

router = APIRouter(prefix="/live-sessions", tags=["live-sessions"])


@router.post(
    "",
    response_model=LiveSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_live_session(
    payload: LiveSessionCreateRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    service = LiveSessionService(session=db)
    session, is_created = service.create_live_session(
        meeting_id=payload.meeting_id,
        user_id=current_user.id,
        tab_session_uuid=payload.tab_session_uuid,
    )

    if not is_created:
        response.status_code = status.HTTP_200_OK

    return session


# ─── Phase 5D: Finalize DOM Transcript ───────────────────────────────────────

@router.post(
    "/{session_id}/finalize",
    response_model=FinalizeTranscriptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Finalize DOM transcript for a live session",
    description=(
        "Persist the finalized DOM-captured transcript for a live session. "
        "Idempotent: same payload returns 200. "
        "Conflicting payload returns 409 SESSION_PAYLOAD_CONFLICT."
    ),
)
def finalize_transcript(
    session_id: uuid.UUID,
    payload: FinalizeTranscriptRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    service = FinalizeService(session=db)
    result, is_created = service.finalize_transcript(
        session_id=session_id,
        user_id=current_user.id,
        payload=payload,
    )

    if not is_created:
        # Idempotent repeat — return 200 instead of 201
        response.status_code = status.HTTP_200_OK

    return result


# ─── Heartbeat ───────────────────────────────────────────────────────────────

@router.post(
    "/{session_id}/heartbeat",
    response_model=LiveSessionHeartbeatResponse,
    status_code=status.HTTP_200_OK,
    summary="Update live session heartbeat and clock calibration",
)
def session_heartbeat(
    session_id: uuid.UUID,
    payload: LiveSessionHeartbeatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    from datetime import datetime, timezone
    from app.api.v1.error_handlers import LiveSessionNotFoundError
    from app.repositories.live_session_repository import LiveSessionRepository

    live_session_repo = LiveSessionRepository(db)
    session = live_session_repo.get_by_id_and_user(session_id, current_user.id)
    if session is None:
        raise LiveSessionNotFoundError(str(session_id))

    server_now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    updated_session, offset_ms = live_session_repo.update_heartbeat(
        session_id=session_id,
        client_timestamp_ms=payload.client_timestamp_ms,
        server_timestamp_ms=server_now_ms,
    )

    active_count = live_session_repo.count_active_sessions_for_meeting(
        session.meeting_id,
        ttl_seconds=90,
    )

    return LiveSessionHeartbeatResponse(
        session_id=session.session_id,
        meeting_id=session.meeting_id,
        status=session.status,
        server_timestamp_ms=server_now_ms,
        clock_offset_ms=offset_ms,
        active_capture_sessions=active_count,
        last_heartbeat_at=session.last_heartbeat_at,
    )

