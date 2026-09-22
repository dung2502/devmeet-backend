from dataclasses import dataclass
from typing import Any

import httpx

from app.integrations.google_oauth import GoogleOAuthClient, GoogleOAuthTokenError

GOOGLE_MEET_API_BASE_URL = "https://meet.googleapis.com/v2"


class GoogleMeetError(Exception):
    pass


class GoogleMeetAuthenticationError(GoogleMeetError):
    pass


class GoogleMeetHTTPError(GoogleMeetError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class GoogleMeetClientError(GoogleMeetHTTPError):
    pass


class GoogleMeetServerError(GoogleMeetHTTPError):
    pass


class GoogleMeetMalformedResponseError(GoogleMeetError):
    pass


@dataclass(frozen=True)
class GoogleMeetListResponse:
    items: list[dict[str, Any]]
    next_page_token: str | None
    raw: dict[str, Any]


class GoogleMeetClient:
    def __init__(
        self,
        access_token: str,
        oauth_client: GoogleOAuthClient | None = None,
        refresh_token: str | None = None,
        subject: str | None = None,
        http_client: httpx.Client | None = None,
        base_url: str = GOOGLE_MEET_API_BASE_URL,
    ) -> None:
        if not access_token:
            raise GoogleMeetAuthenticationError("Access token is required.")

        self.access_token = access_token
        self.oauth_client = oauth_client
        self.refresh_token = refresh_token
        self.subject = subject
        self.http_client = http_client or httpx.Client(timeout=10.0)
        self.base_url = base_url.rstrip("/")

    def get_conference_record(self, conference_record_name: str) -> dict[str, Any]:
        payload = self._get(conference_record_name)
        self._require_resource(payload, "name")
        return payload

    def list_participants(
        self,
        conference_record_name: str,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> GoogleMeetListResponse:
        return self._list(
            f"{conference_record_name}/participants",
            item_key="participants",
            page_token=page_token,
            page_size=page_size,
        )

    def list_transcripts(
        self,
        conference_record_name: str,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> GoogleMeetListResponse:
        return self._list(
            f"{conference_record_name}/transcripts",
            item_key="transcripts",
            page_token=page_token,
            page_size=page_size,
        )

    def list_transcript_entries(
        self,
        transcript_name: str,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> GoogleMeetListResponse:
        return self._list(
            f"{transcript_name}/entries",
            item_key="transcriptEntries",
            page_token=page_token,
            page_size=page_size,
        )

    def _list(
        self,
        resource_path: str,
        item_key: str,
        page_token: str | None,
        page_size: int | None,
    ) -> GoogleMeetListResponse:
        params: dict[str, str | int] = {}
        if page_token is not None:
            params["pageToken"] = page_token
        if page_size is not None:
            params["pageSize"] = page_size

        payload = self._get(resource_path, params=params)
        items = payload.get(item_key)
        next_page_token = payload.get("nextPageToken")

        if not isinstance(items, list):
            raise GoogleMeetMalformedResponseError("Google Meet list response is malformed.")
        if next_page_token is not None and not isinstance(next_page_token, str):
            raise GoogleMeetMalformedResponseError("Google Meet pagination token is malformed.")
        if not all(isinstance(item, dict) for item in items):
            raise GoogleMeetMalformedResponseError("Google Meet list item is malformed.")

        return GoogleMeetListResponse(
            items=items,
            next_page_token=next_page_token,
            raw=payload,
        )

    def _get(
        self,
        resource_path: str,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        response = self._send_get(resource_path, params=params)

        if response.status_code == 401 and self._can_refresh_token():
            self._refresh_access_token()
            response = self._send_get(resource_path, params=params)

        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GoogleMeetMalformedResponseError(
                "Google Meet response is not valid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise GoogleMeetMalformedResponseError("Google Meet response is malformed.")

        return payload

    def _send_get(
        self,
        resource_path: str,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        return self.http_client.get(
            f"{self.base_url}/{resource_path.lstrip('/')}",
            headers={"Authorization": f"Bearer {self.access_token}"},
            params=params,
        )

    def _can_refresh_token(self) -> bool:
        return self.oauth_client is not None and (
            self.refresh_token is not None or self.subject is not None
        )

    def _refresh_access_token(self) -> None:
        if self.oauth_client is None:
            raise GoogleMeetAuthenticationError("Google OAuth client is required.")

        try:
            if self.refresh_token is not None:
                tokens = self.oauth_client.refresh_access_token(
                    self.refresh_token,
                    subject=self.subject,
                )
            elif self.subject is not None:
                tokens = self.oauth_client.refresh_access_token_for_subject(self.subject)
            else:
                raise GoogleMeetAuthenticationError("Refresh token is required.")
        except GoogleOAuthTokenError as exc:
            raise GoogleMeetAuthenticationError("Google OAuth token refresh failed.") from exc

        self.access_token = tokens.access_token
        if tokens.refresh_token is not None:
            self.refresh_token = tokens.refresh_token

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise GoogleMeetAuthenticationError("Google Meet authentication failed.")
        if 400 <= response.status_code < 500:
            raise GoogleMeetClientError(
                response.status_code,
                "Google Meet API client request failed.",
            )
        if response.status_code >= 500:
            raise GoogleMeetServerError(
                response.status_code,
                "Google Meet API server request failed.",
            )

    @staticmethod
    def _require_resource(payload: dict[str, Any], key: str) -> None:
        if not isinstance(payload.get(key), str):
            raise GoogleMeetMalformedResponseError("Google Meet resource is malformed.")
