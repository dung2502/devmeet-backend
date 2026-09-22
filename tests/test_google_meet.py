from collections.abc import Callable

import httpx
import pytest

from app.integrations import (
    GoogleMeetAuthenticationError,
    GoogleMeetClient,
    GoogleMeetClientError,
    GoogleMeetMalformedResponseError,
    GoogleMeetServerError,
    GoogleOAuthClient,
)


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    access_token: str = "access-token-1",
    oauth_client: GoogleOAuthClient | None = None,
    refresh_token: str | None = None,
    subject: str | None = None,
) -> GoogleMeetClient:
    return GoogleMeetClient(
        access_token=access_token,
        oauth_client=oauth_client,
        refresh_token=refresh_token,
        subject=subject,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def make_oauth_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GoogleOAuthClient:
    return GoogleOAuthClient(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8000/api/v1/auth/google/callback",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_get_conference_record_uses_endpoint_authorization_and_parses_response() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            200,
            json={
                "name": "conferenceRecords/abc-123",
                "space": "spaces/space-1",
                "startTime": "2026-08-21T09:00:00Z",
                "endTime": "2026-08-21T10:00:00Z",
            },
        )

    client = make_client(handler)

    record = client.get_conference_record("conferenceRecords/abc-123")

    assert captured_request is not None
    assert str(captured_request.url) == (
        "https://meet.googleapis.com/v2/conferenceRecords/abc-123"
    )
    assert captured_request.headers["Authorization"] == "Bearer access-token-1"
    assert record["name"] == "conferenceRecords/abc-123"
    assert record["space"] == "spaces/space-1"


def test_list_participants_uses_endpoint_query_params_and_pagination() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            200,
            json={
                "participants": [
                    {
                        "name": "conferenceRecords/abc-123/participants/p1",
                        "signedinUser": {"displayName": "Nguyen Van A"},
                    }
                ],
                "nextPageToken": "next-page",
            },
        )

    client = make_client(handler)

    response = client.list_participants(
        "conferenceRecords/abc-123",
        page_token="page-1",
        page_size=50,
    )

    assert captured_request is not None
    assert str(captured_request.url) == (
        "https://meet.googleapis.com/v2/conferenceRecords/abc-123/participants"
        "?pageToken=page-1&pageSize=50"
    )
    assert captured_request.headers["Authorization"] == "Bearer access-token-1"
    assert response.items[0]["name"] == "conferenceRecords/abc-123/participants/p1"
    assert response.next_page_token == "next-page"


def test_list_transcripts_uses_endpoint_and_parses_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://meet.googleapis.com/v2/conferenceRecords/abc-123/transcripts"
            "?pageToken=page-1&pageSize=25"
        )
        return httpx.Response(
            200,
            json={
                "transcripts": [
                    {
                        "name": "conferenceRecords/abc-123/transcripts/t1",
                        "state": "FILE_GENERATED",
                    }
                ],
                "nextPageToken": "page-2",
            },
        )

    client = make_client(handler)

    response = client.list_transcripts(
        "conferenceRecords/abc-123",
        page_token="page-1",
        page_size=25,
    )

    assert response.items == [
        {
            "name": "conferenceRecords/abc-123/transcripts/t1",
            "state": "FILE_GENERATED",
        }
    ]
    assert response.next_page_token == "page-2"


def test_list_transcript_entries_uses_endpoint_and_parses_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://meet.googleapis.com/v2/conferenceRecords/abc-123/transcripts/t1"
            "/entries?pageToken=page-1&pageSize=100"
        )
        return httpx.Response(
            200,
            json={
                "transcriptEntries": [
                    {
                        "name": "conferenceRecords/abc-123/transcripts/t1/entries/e1",
                        "participant": "conferenceRecords/abc-123/participants/p1",
                        "text": "Hello team.",
                        "languageCode": "en-US",
                    }
                ],
                "nextPageToken": "page-2",
            },
        )

    client = make_client(handler)

    response = client.list_transcript_entries(
        "conferenceRecords/abc-123/transcripts/t1",
        page_token="page-1",
        page_size=100,
    )

    assert response.items[0]["name"] == (
        "conferenceRecords/abc-123/transcripts/t1/entries/e1"
    )
    assert response.next_page_token == "page-2"


@pytest.mark.parametrize("status_code", [401, 403])
def test_authentication_errors_raise_authentication_error(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "auth failed"})

    client = make_client(handler)

    with pytest.raises(GoogleMeetAuthenticationError):
        client.get_conference_record("conferenceRecords/abc-123")


@pytest.mark.parametrize("status_code", [404, 429])
def test_client_errors_raise_client_error(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "client failed"})

    client = make_client(handler)

    with pytest.raises(GoogleMeetClientError) as exc_info:
        client.get_conference_record("conferenceRecords/abc-123")

    assert exc_info.value.status_code == status_code


def test_server_errors_raise_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "server failed"})

    client = make_client(handler)

    with pytest.raises(GoogleMeetServerError) as exc_info:
        client.get_conference_record("conferenceRecords/abc-123")

    assert exc_info.value.status_code == 503


def test_malformed_resource_response_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"space": "spaces/space-1"})

    client = make_client(handler)

    with pytest.raises(GoogleMeetMalformedResponseError):
        client.get_conference_record("conferenceRecords/abc-123")


def test_malformed_list_response_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"participants": {"not": "a-list"}})

    client = make_client(handler)

    with pytest.raises(GoogleMeetMalformedResponseError):
        client.list_participants("conferenceRecords/abc-123")


def test_expired_access_token_uses_oauth_refresh_path_and_retries() -> None:
    meet_authorization_headers: list[str] = []

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        assert "refresh_token=refresh-token-1" in request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": "access-token-2",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    def meet_handler(request: httpx.Request) -> httpx.Response:
        meet_authorization_headers.append(request.headers["Authorization"])
        if len(meet_authorization_headers) == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"name": "conferenceRecords/abc-123"})

    oauth_client = make_oauth_client(oauth_handler)
    client = make_client(
        meet_handler,
        oauth_client=oauth_client,
        refresh_token="refresh-token-1",
    )

    record = client.get_conference_record("conferenceRecords/abc-123")

    assert record["name"] == "conferenceRecords/abc-123"
    assert meet_authorization_headers == [
        "Bearer access-token-1",
        "Bearer access-token-2",
    ]


def test_oauth_token_failure_propagates_as_authentication_error() -> None:
    def oauth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    def meet_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "expired"})

    oauth_client = make_oauth_client(oauth_handler)
    client = make_client(
        meet_handler,
        oauth_client=oauth_client,
        refresh_token="refresh-token-1",
    )

    with pytest.raises(GoogleMeetAuthenticationError):
        client.get_conference_record("conferenceRecords/abc-123")


def test_google_meet_errors_do_not_expose_tokens_or_client_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": "auth failed",
                "access_token": "access-token-1",
                "client_secret": "test-client-secret",
            },
        )

    client = make_client(handler)

    with pytest.raises(GoogleMeetAuthenticationError) as exc_info:
        client.get_conference_record("conferenceRecords/abc-123")

    message = str(exc_info.value)
    assert "access-token-1" not in message
    assert "test-client-secret" not in message
