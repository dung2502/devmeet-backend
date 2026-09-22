import uuid
from sqlalchemy.orm import Session

from app.models.live_session import LiveSession
from app.repositories.live_session_repository import LiveSessionRepository


class LiveSessionService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = LiveSessionRepository(session)

    def create_live_session(
        self,
        meeting_id: uuid.UUID,
        user_id: uuid.UUID,
        tab_session_uuid: uuid.UUID,
    ) -> tuple[LiveSession, bool]:
        """
        Creates a new live session, resolves idempotent existing session,
        or supersedes active sessions for the same user and meeting.

        Returns (LiveSession, is_created).
        """
        return self.repository.create_or_get_or_supersede_session(
            meeting_id=meeting_id,
            user_id=user_id,
            tab_session_uuid=tab_session_uuid,
        )
