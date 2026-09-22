from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth import AuthenticationError
from app.integrations.google_meet import (
    GoogleMeetAuthenticationError,
    GoogleMeetClientError,
    GoogleMeetMalformedResponseError,
    GoogleMeetServerError,
)
from app.integrations.n8n import (
    N8nClientError,
    N8nTimeoutError,
)
from app.services.meeting_ai import MeetingAIProcessingConflictError
from app.services.participant_sync import MeetingNotFoundError
from app.services.transcript_sync import TranscriptNotReadyError
from app.services.transcript_token_optimizer import TranscriptInputTooLargeError


# ─── Phase 5D Exception Classes ───────────────────────────────────────────────

class LiveSessionNotFoundError(Exception):
    """Raised when a live session does not exist or is not accessible."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        msg = f"Live session not found: {session_id}" if session_id else "Live session not found."
        super().__init__(msg)


class SessionPayloadConflictError(Exception):
    """Raised when a different finalized transcript payload already exists for the session."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        msg = (
            f"A different finalized transcript payload has already been recorded "
            f"for session {session_id}."
            if session_id
            else "Session payload conflict: a different transcript has already been finalized."
        )
        super().__init__(msg)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthenticationError)
    async def auth_error_handler(
        request: Request, exc: AuthenticationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": None,
                }
            },
        )

    @app.exception_handler(TranscriptNotReadyError)
    async def transcript_not_ready_handler(
        request: Request, exc: TranscriptNotReadyError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": exc.error_code,
                    "message": exc.message,
                    "details": None,
                }
            },
        )

    @app.exception_handler(MeetingNotFoundError)
    async def meeting_not_found_handler(
        request: Request, exc: MeetingNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "MEETING_NOT_FOUND",
                    "message": str(exc),
                    "details": None,
                }
            },
        )

    @app.exception_handler(GoogleMeetAuthenticationError)
    async def google_auth_handler(
        request: Request, exc: GoogleMeetAuthenticationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "GOOGLE_AUTH_FAILED",
                    "message": str(exc),
                    "details": None,
                }
            },
        )

    @app.exception_handler(GoogleMeetClientError)
    async def google_client_error_handler(
        request: Request, exc: GoogleMeetClientError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "GOOGLE_API_CLIENT_ERROR",
                    "message": str(exc),
                    "details": None,
                }
            },
        )

    @app.exception_handler(GoogleMeetServerError)
    async def google_server_error_handler(
        request: Request, exc: GoogleMeetServerError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "GOOGLE_API_SERVER_ERROR",
                    "message": str(exc),
                    "details": None,
                }
            },
        )

    @app.exception_handler(GoogleMeetMalformedResponseError)
    async def google_malformed_handler(
        request: Request, exc: GoogleMeetMalformedResponseError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "GOOGLE_API_MALFORMED_RESPONSE",
                    "message": str(exc),
                    "details": None,
                }
            },
        )

    @app.exception_handler(TranscriptInputTooLargeError)
    async def transcript_too_large_handler(
        request: Request, exc: TranscriptInputTooLargeError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.error_code,
                    "message": exc.message,
                    "details": {
                        "character_count": exc.character_count,
                        "max_chars": exc.max_chars,
                    },
                }
            },
        )

    @app.exception_handler(MeetingAIProcessingConflictError)
    async def meeting_ai_conflict_handler(
        request: Request, exc: MeetingAIProcessingConflictError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.error_code,
                    "message": exc.message,
                    "details": None,
                }
            },
        )

    @app.exception_handler(N8nTimeoutError)
    async def n8n_timeout_handler(
        request: Request, exc: N8nTimeoutError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.error_code,
                    "message": exc.message,
                    "details": None,
                }
            },
        )

    @app.exception_handler(N8nClientError)
    async def n8n_client_error_handler(
        request: Request, exc: N8nClientError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.error_code,
                    "message": exc.message,
                    "details": None,
                }
            },
        )

    @app.exception_handler(LiveSessionNotFoundError)
    async def live_session_not_found_handler(
        request: Request, exc: LiveSessionNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "LIVE_SESSION_NOT_FOUND",
                    "message": str(exc),
                    "details": None,
                }
            },
        )

    @app.exception_handler(SessionPayloadConflictError)
    async def session_payload_conflict_handler(
        request: Request, exc: SessionPayloadConflictError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "SESSION_PAYLOAD_CONFLICT",
                    "message": str(exc),
                    "details": None,
                }
            },
        )
