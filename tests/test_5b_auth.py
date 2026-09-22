import uuid
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import AuthenticationError, GoogleIdentity, GoogleIdentityResolver, get_current_user
from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import User
from app.repositories import UserRepository


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_b1_no_authorization_in_production_returns_401(
    client: TestClient
) -> None:
    with patch("app.auth.get_settings") as mock_settings:
        mock_settings.return_value.app_env = "production"
        mock_settings.return_value.allow_dev_auth_bypass = False

        res = client.post(
            "/api/v1/live-sessions",
            json={
                "meeting_id": str(uuid.uuid4()),
                "tab_session_uuid": str(uuid.uuid4()),
            },
        )
        assert res.status_code == 401
        data = res.json()
        assert "error" in data
        assert data["error"]["code"] in ("UNAUTHORIZED", "MISSING_TOKEN")


def test_b2_invalid_bearer_token_returns_401(
    client: TestClient
) -> None:
    with patch.object(GoogleIdentityResolver, "resolve_access_token", side_effect=AuthenticationError("Invalid or expired Google Access Token.", code="INVALID_TOKEN")):
        res = client.post(
            "/api/v1/live-sessions",
            headers={"Authorization": "Bearer invalid-google-token-xyz"},
            json={
                "meeting_id": str(uuid.uuid4()),
                "tab_session_uuid": str(uuid.uuid4()),
            },
        )
        assert res.status_code == 401
        data = res.json()
        assert data["error"]["code"] == "INVALID_TOKEN"


def test_b3_expired_or_invalid_credential_returns_401(
    client: TestClient
) -> None:
    with patch.object(GoogleIdentityResolver, "resolve_access_token", side_effect=AuthenticationError("Google authentication failed with status 401.", code="INVALID_TOKEN")):
        res = client.post(
            "/api/v1/live-sessions",
            headers={"Authorization": "Bearer expired-token-123"},
            json={
                "meeting_id": str(uuid.uuid4()),
                "tab_session_uuid": str(uuid.uuid4()),
            },
        )
        assert res.status_code == 401


def test_b4_authenticated_google_identity_resolves_to_existing_user(
    client: TestClient, db_session: Session
) -> None:
    user_repo = UserRepository(db_session)
    existing_user = user_repo.create(
        {
            "google_user_id": "google-sub-known-999",
            "email": "known.user@example.com",
            "display_name": "Known User",
        }
    )

    mock_identity = GoogleIdentity(
        google_user_id="google-sub-known-999",
        email="known.user@example.com",
        display_name="Known User",
    )

    with patch.object(GoogleIdentityResolver, "resolve_access_token", return_value=mock_identity):
        meeting_res = client.post(
            "/api/v1/meetings/sync",
            headers={"Authorization": "Bearer valid-token-known"},
            json={"meeting_url": "https://meet.google.com/auth-test-room"},
        )
        assert meeting_res.status_code == 200
        assert meeting_res.json()["user_id"] == str(existing_user.id)


def test_b5_unknown_google_identity_creates_user(
    client: TestClient, db_session: Session
) -> None:
    new_sub = f"google-sub-new-{uuid.uuid4().hex[:8]}"
    mock_identity = GoogleIdentity(
        google_user_id=new_sub,
        email="brand_new_user@example.com",
        display_name="Brand New User",
    )

    with patch.object(GoogleIdentityResolver, "resolve_access_token", return_value=mock_identity):
        meeting_res = client.post(
            "/api/v1/meetings/sync",
            headers={"Authorization": "Bearer valid-token-brand-new"},
            json={"meeting_url": "https://meet.google.com/new-user-room"},
        )
        assert meeting_res.status_code == 200
        created_user_id = meeting_res.json()["user_id"]

        user_repo = UserRepository(db_session)
        user = user_repo.get_by_id(uuid.UUID(created_user_id))
        assert user is not None
        assert user.google_user_id == new_sub
        assert user.email == "brand_new_user@example.com"


def test_b6_client_provided_user_id_cannot_override_authenticated_user(
    client: TestClient, db_session: Session
) -> None:
    user_repo = UserRepository(db_session)
    auth_user = user_repo.create(
        {
            "google_user_id": "google-sub-authenticated-user",
            "email": "authenticated@example.com",
            "display_name": "Auth User",
        }
    )
    fake_user_id = uuid.uuid4()

    mock_identity = GoogleIdentity(
        google_user_id="google-sub-authenticated-user",
        email="authenticated@example.com",
        display_name="Auth User",
    )

    with patch.object(GoogleIdentityResolver, "resolve_access_token", return_value=mock_identity):
        payload = {
            "user_id": str(fake_user_id),
            "meeting_url": "https://meet.google.com/spoof-attempt",
        }
        res = client.post(
            "/api/v1/meetings/sync",
            headers={"Authorization": "Bearer valid-token-spoof"},
            json=payload,
        )
        assert res.status_code == 200
        assert res.json()["user_id"] == str(auth_user.id)
        assert res.json()["user_id"] != str(fake_user_id)


def test_b7_explicit_test_auth_fixture_works_only_in_test_dev(
    client: TestClient
) -> None:
    with patch("app.auth.get_settings") as mock_settings:
        mock_settings.return_value.app_env = "test"
        mock_settings.return_value.allow_dev_auth_bypass = True

        res = client.post(
            "/api/v1/meetings/sync",
            json={"meeting_url": "https://meet.google.com/dev-bypass-room"},
        )
        assert res.status_code == 200


def test_b8_production_path_does_not_silently_use_default_user(
    client: TestClient
) -> None:
    with patch("app.auth.get_settings") as mock_settings:
        mock_settings.return_value.app_env = "production"
        mock_settings.return_value.allow_dev_auth_bypass = False

        res = client.post(
            "/api/v1/meetings/sync",
            json={"meeting_url": "https://meet.google.com/prod-no-fallback"},
        )
        assert res.status_code == 401


def test_b9_blank_or_whitespace_bearer_token_returns_401(
    client: TestClient
) -> None:
    res = client.post(
        "/api/v1/live-sessions",
        headers={"Authorization": "Bearer   "},
        json={"meeting_id": str(uuid.uuid4()), "tab_session_uuid": str(uuid.uuid4())},
    )
    assert res.status_code == 401
    assert res.json()["error"]["code"] in ("UNAUTHORIZED", "MISSING_TOKEN")


def test_b10_malformed_auth_header_returns_401(
    client: TestClient
) -> None:
    res = client.post(
        "/api/v1/live-sessions",
        headers={"Authorization": "Basic some_basic_credentials"},
        json={"meeting_id": str(uuid.uuid4()), "tab_session_uuid": str(uuid.uuid4())},
    )
    assert res.status_code == 401
    assert res.json()["error"]["code"] in ("UNAUTHORIZED", "INVALID_TOKEN")
