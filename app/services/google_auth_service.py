from dataclasses import dataclass
from typing import Any

import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.config import get_settings


class GoogleVerificationError(Exception):
    def __init__(self, message: str, code: str = "GOOGLE_AUTH_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class GoogleProfile:
    sub: str
    email: str
    name: str | None = None
    picture: str | None = None


class GoogleAuthService:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self.http_client = http_client or httpx.Client(timeout=10.0)

    def verify_id_token(
        self,
        token_str: str,
        expected_client_id: str | None = None,
    ) -> GoogleProfile:
        """
        Verify a Google ID Token offline using Google's public key certs.
        """
        if not token_str or not token_str.strip():
            raise GoogleVerificationError("Google ID Token is missing or empty.", code="MISSING_TOKEN")

        settings = get_settings()
        client_id = expected_client_id or settings.google_client_id or None

        try:
            request = google_requests.Request()
            payload: dict[str, Any] = google_id_token.verify_oauth2_token(
                token_str.strip(),
                request,
                audience=client_id,
                clock_skew_in_seconds=10,
            )
        except Exception as exc:
            raise GoogleVerificationError(f"Invalid Google ID Token: {exc}", code="INVALID_ID_TOKEN") from exc

        sub = payload.get("sub")
        email = payload.get("email")
        if not sub or not email:
            raise GoogleVerificationError("Incomplete Google ID Token claims (missing sub or email).", code="INVALID_PROFILE")

        return GoogleProfile(
            sub=str(sub),
            email=str(email),
            name=payload.get("name"),
            picture=payload.get("picture"),
        )

    def verify_access_token(self, token_str: str) -> GoogleProfile:
        """
        Verify a Google OAuth Access Token by calling Google UserInfo endpoint.
        """
        if not token_str or not token_str.strip():
            raise GoogleVerificationError("Google Access Token is missing or empty.", code="MISSING_TOKEN")

        url = "https://www.googleapis.com/oauth2/v3/userinfo"
        try:
            resp = self.http_client.get(
                url,
                headers={"Authorization": f"Bearer {token_str.strip()}"},
            )
        except Exception as exc:
            raise GoogleVerificationError(f"Failed to reach Google UserInfo: {exc}", code="GOOGLE_NETWORK_ERROR") from exc

        if resp.status_code in (401, 403):
            raise GoogleVerificationError("Invalid or expired Google Access Token.", code="INVALID_ACCESS_TOKEN")
        if resp.status_code >= 400:
            raise GoogleVerificationError(f"Google UserInfo returned HTTP {resp.status_code}.", code="GOOGLE_AUTH_FAILED")

        try:
            payload: dict[str, Any] = resp.json()
        except Exception as exc:
            raise GoogleVerificationError("Malformed JSON response from Google UserInfo.", code="GOOGLE_AUTH_FAILED") from exc

        sub = payload.get("sub")
        email = payload.get("email")
        if not sub or not email:
            raise GoogleVerificationError("Incomplete Google user profile response (missing sub or email).", code="INVALID_PROFILE")

        return GoogleProfile(
            sub=str(sub),
            email=str(email),
            name=payload.get("name"),
            picture=payload.get("picture"),
        )
