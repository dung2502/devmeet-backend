import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models.auth_session import AuthSession
from app.models.user import User
from app.repositories.auth_session_repository import AuthSessionRepository
from app.services.google_auth_service import GoogleProfile, GoogleVerificationError


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_google_id_token_login_success_web(client: TestClient, db_session: Session) -> None:
    mock_profile = GoogleProfile(
        sub="google-sub-web-1001",
        email="webuser@example.com",
        name="Web Dashboard User",
        picture="https://lh3.googleusercontent.com/a/pic1001",
    )

    with patch("app.api.v1.auth.GoogleAuthService.verify_id_token", return_value=mock_profile):
        resp = client.post(
            "/api/v1/auth/google",
            json={
                "credential_type": "google_id_token",
                "credential": "mock-valid-id-token-xyz",
                "client_type": "WEB",
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success"] is True
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 1800  # 30 mins
        assert data["refresh_token"] is None  # Web refresh token is in HttpOnly cookie
        assert data["user"]["email"] == "webuser@example.com"
        assert data["user"]["google_user_id"] == "google-sub-web-1001"
        assert data["user"]["picture"] == "https://lh3.googleusercontent.com/a/pic1001"

        # Check Set-Cookie header for devmeet_refresh_token
        set_cookie = resp.headers.get("set-cookie", "")
        assert "devmeet_refresh_token=" in set_cookie
        assert "HttpOnly" in set_cookie

        # Verify user in database
        user = db_session.scalar(select(User).where(User.google_user_id == "google-sub-web-1001"))
        assert user is not None
        assert user.email == "webuser@example.com"
        assert user.picture == "https://lh3.googleusercontent.com/a/pic1001"

        # Verify session record in database
        session_record = db_session.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
        assert session_record is not None
        assert session_record.client_type == "WEB"
        assert session_record.revoked_at is None


def test_google_access_token_login_success_extension(client: TestClient, db_session: Session) -> None:
    mock_profile = GoogleProfile(
        sub="google-sub-ext-2002",
        email="extuser@example.com",
        name="Extension User",
        picture=None,
    )

    with patch("app.api.v1.auth.GoogleAuthService.verify_access_token", return_value=mock_profile):
        resp = client.post(
            "/api/v1/auth/google",
            json={
                "credential_type": "google_access_token",
                "credential": "ya29.mock-google-oauth-access-token",
                "client_type": "EXTENSION",
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success"] is True
        assert "access_token" in data
        assert data["refresh_token"] is not None  # Extension gets refresh token in JSON body
        assert len(data["refresh_token"]) > 30

        # Verify session record in database
        user = db_session.scalar(select(User).where(User.google_user_id == "google-sub-ext-2002"))
        assert user is not None
        session_record = db_session.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
        assert session_record is not None
        assert session_record.client_type == "EXTENSION"


def test_get_current_user_me_with_application_jwt(client: TestClient) -> None:
    mock_profile = GoogleProfile(
        sub="google-sub-jwt-3003",
        email="jwtuser@example.com",
        name="JWT User",
        picture="https://example.com/avatar.png",
    )

    with patch("app.api.v1.auth.GoogleAuthService.verify_id_token", return_value=mock_profile):
        login_resp = client.post(
            "/api/v1/auth/google",
            json={
                "credential_type": "google_id_token",
                "credential": "mock-token-jwt-3003",
                "client_type": "WEB",
            },
        )
        assert login_resp.status_code == 200
        access_token = login_resp.json()["access_token"]

    # Call GET /api/v1/users/me using Application JWT
    me_resp = client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me_resp.status_code == 200, me_resp.text
    me_data = me_resp.json()
    assert me_data["google_user_id"] == "google-sub-jwt-3003"
    assert me_data["email"] == "jwtuser@example.com"
    assert me_data["display_name"] == "JWT User"
    assert me_data["picture"] == "https://example.com/avatar.png"


def test_get_current_user_me_with_revoked_session(client: TestClient, db_session: Session) -> None:
    mock_profile = GoogleProfile(
        sub="google-sub-revoke-4004",
        email="revokeuser@example.com",
        name="Revoke User",
    )

    with patch("app.api.v1.auth.GoogleAuthService.verify_id_token", return_value=mock_profile):
        login_resp = client.post(
            "/api/v1/auth/google",
            json={
                "credential_type": "google_id_token",
                "credential": "mock-token-revoke",
                "client_type": "WEB",
            },
        )
        access_token = login_resp.json()["access_token"]
        user_id = uuid.UUID(login_resp.json()["user"]["id"])

    # Revoke the session in database
    session_repo = AuthSessionRepository(db_session)
    session_record = db_session.scalar(select(AuthSession).where(AuthSession.user_id == user_id))
    assert session_record is not None
    session_repo.revoke(session_record.id)

    # Calling /users/me must return 401 with SESSION_REVOKED
    me_resp = client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me_resp.status_code == 401
    assert me_resp.json()["error"]["code"] == "SESSION_REVOKED"


def test_invalid_google_credential_returns_401(client: TestClient) -> None:
    with patch(
        "app.api.v1.auth.GoogleAuthService.verify_id_token",
        side_effect=GoogleVerificationError("Token signature invalid", code="INVALID_ID_TOKEN"),
    ):
        resp = client.post(
            "/api/v1/auth/google",
            json={
                "credential_type": "google_id_token",
                "credential": "invalid-token-string",
                "client_type": "WEB",
            },
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["detail"]["code"] == "INVALID_ID_TOKEN"
