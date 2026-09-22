import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models.auth_session import AuthSession
from app.models.user import User
from app.services.google_auth_service import GoogleProfile
from app.services.token_service import TokenService


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _create_user_and_session(
    db_session: Session,
    client_type: str = "WEB",
    sub: str = "google-sub-user-b",
    email: str = "user_b@example.com",
) -> tuple[User, AuthSession, str]:
    """Helper to create a user, session, and return (user, auth_session, raw_refresh_token)."""
    user = db_session.scalar(select(User).where(User.google_user_id == sub))
    if not user:
        user = User(
            google_user_id=sub,
            email=email,
            display_name="User Phase B",
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

    raw_token, token_hash = TokenService.create_refresh_token()
    now_utc = datetime.now(timezone.utc)
    session_obj = AuthSession(
        user_id=user.id,
        client_type=client_type,
        refresh_token_hash=token_hash,
        expires_at=now_utc + timedelta(days=30),
    )
    db_session.add(session_obj)
    db_session.commit()
    db_session.refresh(session_obj)
    return user, session_obj, raw_token


def test_refresh_web_client_rotates_cookie(client: TestClient, db_session: Session) -> None:
    user, session_obj, raw_token = _create_user_and_session(db_session, client_type="WEB")
    original_hash = session_obj.refresh_token_hash

    # Call refresh passing refresh token in cookie
    client.cookies.set("devmeet_refresh_token", raw_token)
    resp = client.post("/api/v1/auth/refresh")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 1800
    assert data["refresh_token"] is None  # Web gets rotated token via cookie, not body

    # Verify new cookie is set with rotated token
    set_cookie = resp.headers.get("set-cookie", "")
    assert "devmeet_refresh_token=" in set_cookie
    new_cookie_val = resp.cookies.get("devmeet_refresh_token")
    assert new_cookie_val is not None
    assert new_cookie_val != raw_token

    # Verify session in DB
    db_session.refresh(session_obj)
    assert session_obj.refresh_token_hash != original_hash
    assert session_obj.previous_refresh_token_hash == original_hash
    assert session_obj.revoked_at is None


def test_refresh_extension_client_rotates_body(client: TestClient, db_session: Session) -> None:
    user, session_obj, raw_token = _create_user_and_session(db_session, client_type="EXTENSION")
    original_hash = session_obj.refresh_token_hash

    # Extension passes refresh_token in JSON body
    resp = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": raw_token},
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert "access_token" in data
    assert data["refresh_token"] is not None
    assert data["refresh_token"] != raw_token

    # Verify database state
    db_session.refresh(session_obj)
    assert session_obj.refresh_token_hash != original_hash
    assert session_obj.previous_refresh_token_hash == original_hash


def test_rotated_token_reuse_attack_revokes_all_sessions(client: TestClient, db_session: Session) -> None:
    user, session1, raw_token1 = _create_user_and_session(
        db_session, client_type="WEB", sub="victim-sub-1", email="victim1@example.com"
    )
    _, session2, _ = _create_user_and_session(
        db_session, client_type="EXTENSION", sub="victim-sub-1", email="victim1@example.com"
    )

    # 1. Normal rotation: session 1 rotates token1 to token2
    client.cookies.set("devmeet_refresh_token", raw_token1)
    resp1 = client.post("/api/v1/auth/refresh")
    assert resp1.status_code == 200

    # 2. Attacker replays raw_token1 (which is now previous_refresh_token_hash)
    client.cookies.set("devmeet_refresh_token", raw_token1)
    attack_resp = client.post("/api/v1/auth/refresh")

    assert attack_resp.status_code == 401
    error_detail = attack_resp.json()["detail"]
    assert error_detail["code"] == "TOKEN_REUSE_DETECTED"

    # 3. Verify ALL sessions for this user are revoked for containment!
    db_session.refresh(session1)
    db_session.refresh(session2)
    assert session1.revoked_at is not None
    assert session2.revoked_at is not None


def test_revoked_session_token_reuse_revokes_all_sessions(client: TestClient, db_session: Session) -> None:
    user, session1, raw_token1 = _create_user_and_session(
        db_session, client_type="WEB", sub="victim-sub-2", email="victim2@example.com"
    )
    _, session2, _ = _create_user_and_session(
        db_session, client_type="WEB", sub="victim-sub-2", email="victim2@example.com"
    )

    # Explicitly revoke session 1
    session1.revoked_at = datetime.now(timezone.utc)
    db_session.commit()

    # Client tries to use token from revoked session 1
    resp = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": raw_token1},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "TOKEN_REUSE_DETECTED"

    # Both sessions must be revoked
    db_session.refresh(session1)
    db_session.refresh(session2)
    assert session1.revoked_at is not None
    assert session2.revoked_at is not None


def test_refresh_expired_token_rejected(client: TestClient, db_session: Session) -> None:
    user, session_obj, raw_token = _create_user_and_session(db_session, client_type="WEB")
    session_obj.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.commit()

    resp = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": raw_token},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "TOKEN_EXPIRED"


def test_refresh_missing_token_rejected(client: TestClient) -> None:
    # Ensure no cookies set
    client.cookies.clear()
    resp = client.post("/api/v1/auth/refresh")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "MISSING_TOKEN"


def test_logout_single_session_with_bearer(client: TestClient, db_session: Session) -> None:
    user, session1, _ = _create_user_and_session(
        db_session, client_type="WEB", sub="logout-user-1", email="logout1@example.com"
    )
    _, session2, _ = _create_user_and_session(
        db_session, client_type="WEB", sub="logout-user-1", email="logout1@example.com"
    )

    access_token1, _ = TokenService.create_access_token(user, session1.id, "WEB")

    resp = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token1}"},
    )

    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Check cookie deletion in response
    set_cookie = resp.headers.get("set-cookie", "")
    assert "devmeet_refresh_token=" in set_cookie
    assert ("Max-Age=0" in set_cookie) or ("expires=" in set_cookie.lower())

    # Session 1 is revoked, Session 2 remains active
    db_session.refresh(session1)
    db_session.refresh(session2)
    assert session1.revoked_at is not None
    assert session2.revoked_at is None


def test_logout_all_sessions(client: TestClient, db_session: Session) -> None:
    user, session1, _ = _create_user_and_session(
        db_session, client_type="WEB", sub="logout-user-2", email="logout2@example.com"
    )
    _, session2, _ = _create_user_and_session(
        db_session, client_type="WEB", sub="logout-user-2", email="logout2@example.com"
    )

    access_token1, _ = TokenService.create_access_token(user, session1.id, "WEB")

    resp = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token1}"},
        json={"all_sessions": True},
    )

    assert resp.status_code == 200

    # Both sessions revoked
    db_session.refresh(session1)
    db_session.refresh(session2)
    assert session1.revoked_at is not None
    assert session2.revoked_at is not None


def test_logout_with_cookie_only(client: TestClient, db_session: Session) -> None:
    user, session_obj, raw_token = _create_user_and_session(db_session, client_type="WEB")

    client.cookies.set("devmeet_refresh_token", raw_token)
    resp = client.post("/api/v1/auth/logout")

    assert resp.status_code == 200
    db_session.refresh(session_obj)
    assert session_obj.revoked_at is not None
