import hashlib
import secrets
import time
import uuid
from typing import Any

import jwt

from app.config import get_settings
from app.models.user import User


class TokenDecodeError(Exception):
    def __init__(self, message: str, code: str = "INVALID_TOKEN") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class TokenService:
    @staticmethod
    def create_access_token(
        user: User,
        session_id: uuid.UUID,
        client_type: str = "WEB",
        custom_expire_seconds: int | None = None,
    ) -> tuple[str, int]:
        """
        Create a signed Application JWT access token.
        Returns (token_string, expires_in_seconds).
        """
        settings = get_settings()
        now = int(time.time())
        ttl_seconds = custom_expire_seconds or (settings.access_token_expire_minutes * 60)
        exp = now + ttl_seconds

        payload: dict[str, Any] = {
            "sub": str(user.id),
            "google_user_id": user.google_user_id,
            "email": user.email,
            "display_name": user.display_name or user.email,
            "avatar_url": user.picture,
            "session_id": str(session_id),
            "client_type": client_type,
            "iss": "devmeeting-ai-auth",
            "iat": now,
            "exp": exp,
        }

        token = jwt.encode(
            payload,
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )
        return token, ttl_seconds

    @staticmethod
    def create_refresh_token() -> tuple[str, str]:
        """
        Generate a cryptographically secure opaque refresh token and its SHA-256 hash.
        Returns (raw_token, token_hash).
        """
        raw_token = secrets.token_urlsafe(48)
        token_hash = TokenService.hash_refresh_token(raw_token)
        return raw_token, token_hash

    @staticmethod
    def hash_refresh_token(raw_token: str) -> str:
        """
        Compute SHA-256 digest of an opaque refresh token.
        """
        return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def hash_token(raw_token: str) -> str:
        """
        Alias for hash_refresh_token.
        """
        return TokenService.hash_refresh_token(raw_token)


    @staticmethod
    def decode_access_token(token_str: str) -> dict[str, Any]:
        """
        Decode and verify an Application JWT access token.
        """
        settings = get_settings()
        try:
            payload = jwt.decode(
                token_str,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm],
                issuer="devmeeting-ai-auth",
            )
            return payload
        except jwt.ExpiredSignatureError as exc:
            raise TokenDecodeError("Access token has expired.", code="TOKEN_EXPIRED") from exc
        except jwt.PyJWTError as exc:
            raise TokenDecodeError(f"Invalid access token: {exc}", code="INVALID_TOKEN") from exc
