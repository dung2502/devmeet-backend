from dataclasses import dataclass
from typing import Any
import uuid
import httpx
from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.auth_session import AuthSession
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.services.token_service import TokenDecodeError, TokenService

GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


class AuthenticationError(Exception):
    def __init__(self, message: str, code: str = "UNAUTHORIZED"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class GoogleIdentity:
    google_user_id: str
    email: str
    display_name: str | None = None


class GoogleIdentityResolver:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self.http_client = http_client or httpx.Client(timeout=10.0)

    def resolve_access_token(self, access_token: str) -> GoogleIdentity:
        if not access_token or not access_token.strip():
            raise AuthenticationError("Access token is required.", code="MISSING_TOKEN")

        try:
            response = self.http_client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token.strip()}"},
            )
        except Exception as exc:
            raise AuthenticationError(f"Failed to verify Google access token: {exc}", code="GOOGLE_AUTH_FAILED") from exc

        if response.status_code == 401 or response.status_code == 403:
            raise AuthenticationError("Invalid or expired Google Access Token.", code="INVALID_TOKEN")
        if response.status_code >= 400:
            raise AuthenticationError(f"Google authentication failed with status {response.status_code}.", code="GOOGLE_AUTH_FAILED")

        try:
            payload: dict[str, Any] = response.json()
        except Exception as exc:
            raise AuthenticationError("Malformed response from Google userinfo.", code="GOOGLE_AUTH_FAILED") from exc

        sub = payload.get("sub")
        email = payload.get("email")
        name = payload.get("name")

        if not sub or not email:
            raise AuthenticationError("Incomplete Google user profile response (missing sub or email).", code="INVALID_PROFILE")

        return GoogleIdentity(
            google_user_id=str(sub),
            email=str(email),
            display_name=name,
        )


def _resolve_dev_user(db: Session) -> User:
    user_repo = UserRepository(db)
    default_sub = "default-dev-user-109823746192837461928"
    default_email = "developer@example.com"
    user = db.scalar(select(User).where(User.google_user_id == default_sub))
    if user is not None:
        return user
    existing_by_email = db.scalar(select(User).where(User.email == default_email))
    if existing_by_email is not None:
        return existing_by_email
    return user_repo.create(
        {
            "google_user_id": default_sub,
            "email": default_email,
            "display_name": "Developer User",
        }
    )


def get_current_user(
    authorization: str | None = Header(None, alias="Authorization"),
    x_google_access_token: str | None = Header(None, alias="X-Google-Access-Token"),
    access_token_query: str | None = Query(None, alias="access_token"),
    db: Session = Depends(get_db),
) -> User:
    settings = get_settings()

    raw_token = None
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw_token = parts[1].strip()
            if not raw_token:
                raise AuthenticationError("Bearer token is empty.", code="MISSING_TOKEN")
        elif len(parts) == 1 and parts[0].lower() == "bearer":
            raise AuthenticationError("Bearer token is empty.", code="MISSING_TOKEN")
        elif len(parts) == 1:
            raw_token = parts[0].strip()
        else:
            raise AuthenticationError("Malformed Authorization header. Must be Bearer token.", code="INVALID_TOKEN")
    elif x_google_access_token:
        raw_token = x_google_access_token.strip()
    elif access_token_query:
        raw_token = access_token_query.strip()

    if not raw_token:
        if settings.allow_dev_auth_bypass and settings.app_env in ("development", "test"):
            return _resolve_dev_user(db)
        raise AuthenticationError("Authorization Bearer token is required.", code="UNAUTHORIZED")

    # 1. Primary path: Application JWT resolution
    if raw_token.count(".") == 2:
        try:
            payload = TokenService.decode_access_token(raw_token)
            user_id_str = payload.get("sub")
            session_id_str = payload.get("session_id")
            if user_id_str:
                user_id = uuid.UUID(user_id_str)
                user = db.get(User, user_id)
                if user is None:
                    raise AuthenticationError("User not found.", code="USER_NOT_FOUND")

                # Verify session has not been revoked
                if session_id_str:
                    auth_session = db.get(AuthSession, uuid.UUID(session_id_str))
                    if auth_session is None or auth_session.revoked_at is not None:
                        raise AuthenticationError("Session has been revoked.", code="SESSION_REVOKED")

                return user
        except TokenDecodeError as exc:
            if exc.code == "TOKEN_EXPIRED":
                raise AuthenticationError("Access token has expired.", code="TOKEN_EXPIRED") from exc
            # If not a valid JWT, let it fall through or raise if intended as JWT
            raise AuthenticationError("Invalid access token.", code="INVALID_TOKEN") from exc

    # 2. Migration fallback: Google OAuth Access Token resolver
    resolver = GoogleIdentityResolver()
    identity = resolver.resolve_access_token(raw_token)

    # Map Google identity to DevMeet User
    user = db.scalar(select(User).where(User.google_user_id == identity.google_user_id))
    if user is None:
        user = db.scalar(select(User).where(User.email == identity.email))
        if user is not None:
            user.google_user_id = identity.google_user_id
            if identity.display_name:
                user.display_name = identity.display_name
            db.commit()
            db.refresh(user)
        else:
            user_repo = UserRepository(db)
            user = user_repo.create(
                {
                    "google_user_id": identity.google_user_id,
                    "email": identity.email,
                    "display_name": identity.display_name,
                }
            )

    return user
