import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models.user import User
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    GoogleAuthRequest,
    GoogleAuthResponse,
    LogoutRequest,
    LogoutResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    UserProfileResponse,
)
from app.services.google_auth_service import GoogleAuthService, GoogleVerificationError
from app.services.token_service import TokenService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/google",
    response_model=GoogleAuthResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign in with Google",
    description=(
        "Authenticates a client using a Google ID Token (Web GIS) or Google OAuth Access Token (Extension). "
        "Verifies identity, provisions user by canonical sub, creates a tracked application session, and issues "
        "a short-lived Application JWT access token and revocable refresh token."
    ),
)
def sign_in_with_google(
    payload: GoogleAuthRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> GoogleAuthResponse:
    settings = get_settings()
    google_service = GoogleAuthService()

    # 1. Verify Google credential based on explicit discriminator
    try:
        if payload.credential_type == "google_id_token":
            profile = google_service.verify_id_token(payload.credential)
        elif payload.credential_type == "google_access_token":
            profile = google_service.verify_access_token(payload.credential)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported credential_type: {payload.credential_type}",
            )
    except GoogleVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    # 2. Find or create user strictly by canonical google_user_id (sub)
    user = db.scalar(select(User).where(User.google_user_id == profile.sub))
    if user is not None:
        if profile.name and profile.name != user.display_name:
            user.display_name = profile.name
        if profile.picture and profile.picture != user.picture:
            user.picture = profile.picture
        if profile.email and profile.email != user.email:
            user.email = profile.email
        db.commit()
        db.refresh(user)
    else:
        user_repo = UserRepository(db)
        user = user_repo.create(
            {
                "google_user_id": profile.sub,
                "email": profile.email,
                "display_name": profile.name,
            }
        )
        if profile.picture:
            user.picture = profile.picture
            db.commit()
            db.refresh(user)

    # 3. Create persistent application session with hashed refresh token
    raw_refresh_token, refresh_token_hash = TokenService.create_refresh_token()
    now_utc = datetime.now(timezone.utc)
    expires_at = now_utc + timedelta(days=settings.refresh_token_expire_days)

    user_agent = request.headers.get("user-agent")
    ip_address = request.client.host if request.client else None

    session_repo = AuthSessionRepository(db)
    auth_session = session_repo.create(
        user_id=user.id,
        client_type=payload.client_type,
        refresh_token_hash=refresh_token_hash,
        expires_at=expires_at,
        user_agent=user_agent,
        ip_address=ip_address,
    )

    # 4. Generate Application JWT access token
    access_token, expires_in = TokenService.create_access_token(
        user=user,
        session_id=auth_session.id,
        client_type=payload.client_type,
    )

    # 5. Delivery profile based on client type
    refresh_token_for_body = None
    if payload.client_type == "WEB":
        is_secure = settings.app_env not in ("development", "test")
        response.set_cookie(
            key="devmeet_refresh_token",
            value=raw_refresh_token,
            max_age=settings.refresh_token_expire_days * 86400,
            httponly=True,
            secure=is_secure,
            samesite="lax",
            path="/api/v1/auth",
        )
    else:
        refresh_token_for_body = raw_refresh_token

    return GoogleAuthResponse(
        success=True,
        access_token=access_token,
        token_type="bearer",
        expires_in=expires_in,
        refresh_token=refresh_token_for_body,
        user=UserProfileResponse.model_validate(user),
    )


@router.post(
    "/extension-session",
    response_model=GoogleAuthResponse,
    status_code=status.HTTP_200_OK,
    summary="Issue dedicated extension session from Web",
    description=(
        "Issues a dedicated Extension Application JWT and revocable refresh token "
        "for the currently authenticated user (Web Bridge Handshake). "
        "The refresh token is delivered in the response body for storage in chrome.storage.local."
    ),
)
def create_extension_session(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GoogleAuthResponse:
    settings = get_settings()
    session_repo = AuthSessionRepository(db)

    # 1. Create tracked session with client_type='EXTENSION'
    raw_refresh_token, refresh_token_hash = TokenService.create_refresh_token()
    now_utc = datetime.now(timezone.utc)
    expires_at = now_utc + timedelta(days=settings.refresh_token_expire_days)

    user_agent = request.headers.get("user-agent")
    ip_address = request.client.host if request.client else None

    auth_session = session_repo.create(
        user_id=current_user.id,
        client_type="EXTENSION",
        refresh_token_hash=refresh_token_hash,
        expires_at=expires_at,
        user_agent=user_agent,
        ip_address=ip_address,
    )

    # 2. Generate Application JWT access token
    access_token, expires_in = TokenService.create_access_token(
        user=current_user,
        session_id=auth_session.id,
        client_type="EXTENSION",
    )

    return GoogleAuthResponse(
        success=True,
        access_token=access_token,
        token_type="bearer",
        expires_in=expires_in,
        refresh_token=raw_refresh_token,
        user=UserProfileResponse.model_validate(current_user),
    )


@router.post(
    "/refresh",
    response_model=RefreshTokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Refresh application session",
    description=(
        "Refreshes an Application JWT using a valid refresh token. "
        "Rotates the refresh token on every call to prevent token replay. "
        "Detects token reuse: if a previously rotated or revoked token is presented, "
        "all active sessions for the user are immediately terminated for security."
    ),
)
def refresh_session(
    request: Request,
    response: Response,
    payload: RefreshTokenRequest | None = Body(default=None),
    db: Session = Depends(get_db),
) -> RefreshTokenResponse:
    settings = get_settings()
    session_repo = AuthSessionRepository(db)

    # 1. Extract raw refresh token: cookie (WEB) takes precedence, then JSON body (EXTENSION)
    raw_token = request.cookies.get("devmeet_refresh_token")
    if not raw_token and payload and payload.refresh_token:
        raw_token = payload.refresh_token.strip()

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_TOKEN", "message": "Refresh token is required via cookie or request body."},
        )

    token_hash = TokenService.hash_token(raw_token)

    # 2. Check primary match: current refresh_token_hash
    auth_session = session_repo.get_by_refresh_token_hash(token_hash)
    if auth_session is not None:
        # Check if session is already revoked
        if auth_session.revoked_at is not None:
            # Breach containment: attempt to use token from a revoked session!
            session_repo.revoke_all_for_user(auth_session.user_id)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "TOKEN_REUSE_DETECTED",
                    "message": "Revoked session token reuse detected. All active sessions terminated.",
                },
            )

        # Check if session has expired
        now_utc = datetime.now(timezone.utc)
        if auth_session.expires_at <= now_utc:
            session_repo.revoke(auth_session.id)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "TOKEN_EXPIRED", "message": "Refresh token has expired. Please sign in again."},
            )

        # Valid session: fetch user and rotate refresh token
        user = db.get(User, auth_session.user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "USER_NOT_FOUND", "message": "Associated user account not found."},
            )

        new_raw_token, new_token_hash = TokenService.create_refresh_token()
        new_expires_at = now_utc + timedelta(days=settings.refresh_token_expire_days)
        session_repo.rotate_refresh_token(
            session_id=auth_session.id,
            new_token_hash=new_token_hash,
            new_expires_at=new_expires_at,
        )

        new_access_token, expires_in = TokenService.create_access_token(
            user=user,
            session_id=auth_session.id,
            client_type=auth_session.client_type,
        )

        # Deliver rotated token based on client type
        refresh_token_for_body = None
        if auth_session.client_type == "WEB":
            is_secure = settings.app_env not in ("development", "test")
            response.set_cookie(
                key="devmeet_refresh_token",
                value=new_raw_token,
                max_age=settings.refresh_token_expire_days * 86400,
                httponly=True,
                secure=is_secure,
                samesite="lax",
                path="/api/v1/auth",
            )
        else:
            refresh_token_for_body = new_raw_token

        return RefreshTokenResponse(
            success=True,
            access_token=new_access_token,
            token_type="bearer",
            expires_in=expires_in,
            refresh_token=refresh_token_for_body,
        )

    # 3. Check secondary match: previous_refresh_token_hash (Token Reuse Attack Detection)
    compromised_session = session_repo.get_by_previous_refresh_token_hash(token_hash)
    if compromised_session is not None:
        # A previously rotated refresh token is being reused!
        session_repo.revoke_all_for_user(compromised_session.user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "TOKEN_REUSE_DETECTED",
                "message": "Rotated refresh token reuse detected. All active sessions have been terminated for security.",
            },
        )

    # 4. No matching token hash found
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "INVALID_TOKEN", "message": "Invalid refresh token."},
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign out and revoke session",
    description=(
        "Revokes the current application session (or all user sessions if all_sessions=True), "
        "and clears the HttpOnly refresh token cookie."
    ),
)
def logout(
    request: Request,
    response: Response,
    payload: LogoutRequest | None = Body(default=None),
    db: Session = Depends(get_db),
) -> LogoutResponse:
    settings = get_settings()
    session_repo = AuthSessionRepository(db)

    # Clear HttpOnly cookie on client
    is_secure = settings.app_env not in ("development", "test")
    response.delete_cookie(
        key="devmeet_refresh_token",
        path="/api/v1/auth",
        httponly=True,
        secure=is_secure,
        samesite="lax",
    )

    all_sessions = payload.all_sessions if payload else False
    session_revoked = False

    # A. Check Bearer token in Authorization header
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.strip().lower().startswith("bearer "):
        token_str = auth_header.strip().split(None, 1)[1].strip()
        if token_str.count(".") == 2:
            try:
                claims = TokenService.decode_access_token(token_str)
                user_id_str = claims.get("sub")
                session_id_str = claims.get("session_id")
                if all_sessions and user_id_str:
                    session_repo.revoke_all_for_user(uuid.UUID(user_id_str))
                    session_revoked = True
                elif session_id_str:
                    session_repo.revoke(uuid.UUID(session_id_str))
                    session_revoked = True
            except Exception:
                pass

    # B. If not yet revoked, check refresh token in cookie or payload
    if not session_revoked:
        raw_token = request.cookies.get("devmeet_refresh_token")
        if not raw_token and payload and payload.refresh_token:
            raw_token = payload.refresh_token.strip()

        if raw_token:
            token_hash = TokenService.hash_token(raw_token)
            auth_session = session_repo.get_by_refresh_token_hash(token_hash)
            if auth_session is not None:
                if all_sessions:
                    session_repo.revoke_all_for_user(auth_session.user_id)
                else:
                    session_repo.revoke(auth_session.id)
                session_revoked = True

    return LogoutResponse(
        success=True,
        message="Đăng xuất thành công, phiên làm việc đã bị thu hồi.",
    )

