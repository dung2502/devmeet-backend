import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.auth_session import AuthSession


class AuthSessionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        user_id: uuid.UUID,
        client_type: str,
        refresh_token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AuthSession:
        auth_session = AuthSession(
            user_id=user_id,
            client_type=client_type,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self.session.add(auth_session)
        self.session.commit()
        self.session.refresh(auth_session)
        return auth_session

    def get_by_id(self, session_id: uuid.UUID) -> AuthSession | None:
        return self.session.get(AuthSession, session_id)

    def get_by_refresh_token_hash(self, refresh_token_hash: str) -> AuthSession | None:
        stmt = select(AuthSession).where(AuthSession.refresh_token_hash == refresh_token_hash)
        return self.session.scalar(stmt)

    def get_by_previous_refresh_token_hash(self, previous_hash: str) -> AuthSession | None:
        stmt = select(AuthSession).where(AuthSession.previous_refresh_token_hash == previous_hash)
        return self.session.scalar(stmt)

    def revoke(self, session_id: uuid.UUID) -> bool:
        session_obj = self.get_by_id(session_id)
        if session_obj is None or session_obj.revoked_at is not None:
            return False
        session_obj.revoked_at = datetime.now(timezone.utc)
        self.session.commit()
        return True

    def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        stmt = (
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        result = self.session.execute(stmt)
        self.session.commit()
        return result.rowcount

    def rotate_refresh_token(
        self,
        session_id: uuid.UUID,
        new_token_hash: str,
        new_expires_at: datetime,
    ) -> bool:
        session_obj = self.get_by_id(session_id)
        if session_obj is None:
            return False
        session_obj.previous_refresh_token_hash = session_obj.refresh_token_hash
        session_obj.refresh_token_hash = new_token_hash
        session_obj.expires_at = new_expires_at
        session_obj.last_used_at = datetime.now(timezone.utc)
        self.session.commit()
        return True
