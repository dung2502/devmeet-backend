from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.config import Settings
from app.integrations import (
    GOOGLE_MEET_READONLY_SCOPE,
    GoogleOAuthClient,
    GoogleOAuthConfigurationError,
    GoogleOAuthTokenError,
    InMemoryGoogleCredentialStore,
)


def make_client(
    handler: httpx.MockTransport | None = None,
    store: InMemoryGoogleCredentialStore | None = None,
) -> GoogleOAuthClient:
    http_client = httpx.Client(transport=handler) if handler is not None else None
    return GoogleOAuthClient(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8000/api/v1/auth/google/callback",
        http_client=http_client,
        credential_store=store,
    )


def form_data(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode())
    return {key: value[0] for key, value in parsed.items()}


def test_authorization_url_uses_expected_config_scope_and_no_secret() -> None:
    client = make_client()

    url = client.build_authorization_url(state="state-123")
    parsed_url = urlparse(url)
    params = parse_qs(parsed_url.query)

    assert parsed_url.scheme == "https"
    assert parsed_url.netloc == "accounts.google.com"
    assert params["client_id"] == ["test-client-id"]
    assert params["redirect_uri"] == [
        "http://localhost:8000/api/v1/auth/google/callback"
    ]
    assert params["scope"] == [GOOGLE_MEET_READONLY_SCOPE]
    assert params["response_type"] == ["code"]
    assert params["access_type"] == ["offline"]
    assert params["include_granted_scopes"] == ["true"]
    assert params["prompt"] == ["consent"]
    assert params["state"] == ["state-123"]
    assert "test-client-secret" not in url


def test_authorization_code_exchange_posts_code_and_stores_refresh_token() -> None:
    captured_form: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_form
        captured_form = form_data(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access-token-1",
                "refresh_token": "refresh-token-1",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": GOOGLE_MEET_READONLY_SCOPE,
            },
        )

    store = InMemoryGoogleCredentialStore()
    client = make_client(httpx.MockTransport(handler), store)

    tokens = client.exchange_authorization_code("auth-code-1", subject="user-1")

    assert captured_form["client_id"] == "test-client-id"
    assert captured_form["client_secret"] == "test-client-secret"
    assert captured_form["code"] == "auth-code-1"
    assert captured_form["grant_type"] == "authorization_code"
    assert captured_form["redirect_uri"] == (
        "http://localhost:8000/api/v1/auth/google/callback"
    )
    assert tokens.access_token == "access-token-1"
    assert tokens.refresh_token == "refresh-token-1"
    assert tokens.expires_in == 3600
    assert tokens.token_type == "Bearer"
    assert store.get_refresh_token("user-1") == "refresh-token-1"


def test_token_refresh_uses_refresh_token_without_new_authorization() -> None:
    captured_form: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_form
        captured_form = form_data(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access-token-2",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    client = make_client(httpx.MockTransport(handler))

    tokens = client.refresh_access_token("refresh-token-1")

    assert captured_form["client_id"] == "test-client-id"
    assert captured_form["client_secret"] == "test-client-secret"
    assert captured_form["grant_type"] == "refresh_token"
    assert captured_form["refresh_token"] == "refresh-token-1"
    assert "code" not in captured_form
    assert "redirect_uri" not in captured_form
    assert tokens.access_token == "access-token-2"
    assert tokens.refresh_token is None


def test_token_refresh_for_subject_uses_server_side_store() -> None:
    store = InMemoryGoogleCredentialStore()
    store.save_refresh_token("user-1", "refresh-token-1")

    def handler(request: httpx.Request) -> httpx.Response:
        assert form_data(request)["refresh_token"] == "refresh-token-1"
        return httpx.Response(
            200,
            json={
                "access_token": "access-token-2",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    client = make_client(httpx.MockTransport(handler), store)

    tokens = client.refresh_access_token_for_subject("user-1")

    assert tokens.access_token == "access-token-2"


def test_token_exchange_failure_does_not_expose_client_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "client_secret": "test-client-secret",
            },
        )

    client = make_client(httpx.MockTransport(handler))

    with pytest.raises(GoogleOAuthTokenError) as exc_info:
        client.exchange_authorization_code("bad-code")

    assert "test-client-secret" not in str(exc_info.value)


def test_token_refresh_failure_does_not_expose_refresh_token_or_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "refresh_token": "refresh-token-1",
                "client_secret": "test-client-secret",
            },
        )

    client = make_client(httpx.MockTransport(handler))

    with pytest.raises(GoogleOAuthTokenError) as exc_info:
        client.refresh_access_token("refresh-token-1")

    assert "refresh-token-1" not in str(exc_info.value)
    assert "test-client-secret" not in str(exc_info.value)


def test_missing_required_oauth_configuration_raises() -> None:
    settings = Settings(
        _env_file=None,
        google_client_id="",
        google_client_secret="",
        google_redirect_uri="",
    )

    with pytest.raises(GoogleOAuthConfigurationError) as exc_info:
        GoogleOAuthClient.from_settings(settings)

    message = str(exc_info.value)
    assert "GOOGLE_CLIENT_ID" in message
    assert "GOOGLE_CLIENT_SECRET" in message
    assert "GOOGLE_REDIRECT_URI" in message


def test_unsupported_scope_raises_configuration_error() -> None:
    with pytest.raises(GoogleOAuthConfigurationError):
        GoogleOAuthClient(
            client_id="test-client-id",
            client_secret="test-client-secret",
            redirect_uri="http://localhost:8000/api/v1/auth/google/callback",
            scopes=("https://www.googleapis.com/auth/drive",),
        )


def test_env_example_contains_only_oauth_placeholders() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")

    assert "GOOGLE_CLIENT_ID=your-google-client-id" in env_example
    assert "GOOGLE_CLIENT_SECRET=your-google-client-secret" in env_example
    assert (
        "GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/auth/google/callback"
        in env_example
    )
    assert "test-client-secret" not in env_example
    assert "refresh-token" not in env_example
