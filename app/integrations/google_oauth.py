from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings, get_settings

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_MEET_READONLY_SCOPE = "https://www.googleapis.com/auth/meetings.space.readonly"
GOOGLE_MEET_CREATED_SCOPE = "https://www.googleapis.com/auth/meetings.space.created"
DEFAULT_GOOGLE_OAUTH_SCOPES = (GOOGLE_MEET_READONLY_SCOPE,)
ALLOWED_GOOGLE_OAUTH_SCOPES = {
    GOOGLE_MEET_READONLY_SCOPE,
    GOOGLE_MEET_CREATED_SCOPE,
}


class GoogleOAuthError(Exception):
    pass


class GoogleOAuthConfigurationError(GoogleOAuthError):
    pass


class GoogleOAuthTokenError(GoogleOAuthError):
    pass


@dataclass(frozen=True)
class GoogleOAuthTokens:
    access_token: str
    token_type: str
    expires_in: int | None = None
    refresh_token: str | None = None
    scope: str | None = None


class InMemoryGoogleCredentialStore:
    def __init__(self) -> None:
        self._refresh_tokens: dict[str, str] = {}

    def save_refresh_token(self, subject: str, refresh_token: str) -> None:
        self._refresh_tokens[subject] = refresh_token

    def get_refresh_token(self, subject: str) -> str | None:
        return self._refresh_tokens.get(subject)


class GoogleOAuthClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: tuple[str, ...] = DEFAULT_GOOGLE_OAUTH_SCOPES,
        http_client: httpx.Client | None = None,
        credential_store: InMemoryGoogleCredentialStore | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.http_client = http_client or httpx.Client(timeout=10.0)
        self.credential_store = credential_store or InMemoryGoogleCredentialStore()
        self._validate_configuration()

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
        credential_store: InMemoryGoogleCredentialStore | None = None,
    ) -> "GoogleOAuthClient":
        settings = settings or get_settings()
        return cls(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            redirect_uri=settings.google_redirect_uri,
            http_client=http_client,
            credential_store=credential_store,
        )

    def build_authorization_url(self, state: str | None = None) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        }
        if state is not None:
            params["state"] = state

        return f"{GOOGLE_AUTHORIZATION_URL}?{urlencode(params)}"

    def exchange_authorization_code(
        self,
        authorization_code: str,
        subject: str | None = None,
    ) -> GoogleOAuthTokens:
        if not authorization_code:
            raise GoogleOAuthTokenError("Authorization code is required.")

        response = self.http_client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": authorization_code,
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri,
            },
        )
        tokens = self._parse_token_response(response)

        if subject is not None and tokens.refresh_token is not None:
            self.credential_store.save_refresh_token(subject, tokens.refresh_token)

        return tokens

    def refresh_access_token(
        self,
        refresh_token: str,
        subject: str | None = None,
    ) -> GoogleOAuthTokens:
        if not refresh_token:
            raise GoogleOAuthTokenError("Refresh token is required.")

        response = self.http_client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        tokens = self._parse_token_response(response)

        if subject is not None and tokens.refresh_token is not None:
            self.credential_store.save_refresh_token(subject, tokens.refresh_token)

        return tokens

    def refresh_access_token_for_subject(self, subject: str) -> GoogleOAuthTokens:
        refresh_token = self.credential_store.get_refresh_token(subject)
        if refresh_token is None:
            raise GoogleOAuthTokenError("No refresh token found for subject.")
        return self.refresh_access_token(refresh_token, subject=subject)

    def _validate_configuration(self) -> None:
        missing = []
        for name, value in (
            ("GOOGLE_CLIENT_ID", self.client_id),
            ("GOOGLE_CLIENT_SECRET", self.client_secret),
            ("GOOGLE_REDIRECT_URI", self.redirect_uri),
        ):
            if not value or value.startswith("your-"):
                missing.append(name)

        if missing:
            raise GoogleOAuthConfigurationError(
                f"Missing required Google OAuth configuration: {', '.join(missing)}"
            )

        invalid_scopes = set(self.scopes) - ALLOWED_GOOGLE_OAUTH_SCOPES
        if invalid_scopes:
            raise GoogleOAuthConfigurationError("Unsupported Google OAuth scope.")

    @staticmethod
    def _parse_token_response(response: httpx.Response) -> GoogleOAuthTokens:
        if response.status_code >= 400:
            raise GoogleOAuthTokenError("Google OAuth token request failed.")

        payload: dict[str, Any] = response.json()
        access_token = payload.get("access_token")
        token_type = payload.get("token_type")
        if not access_token or not token_type:
            raise GoogleOAuthTokenError("Google OAuth token response is incomplete.")

        return GoogleOAuthTokens(
            access_token=access_token,
            refresh_token=payload.get("refresh_token"),
            expires_in=payload.get("expires_in"),
            token_type=token_type,
            scope=payload.get("scope"),
        )
