import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GoogleAuthRequest(BaseModel):
    credential_type: Literal["google_id_token", "google_access_token"] = Field(
        ...,
        description="Type of Google credential presented: google_id_token for Web GIS, google_access_token for Chrome Extension.",
    )
    credential: str = Field(
        ...,
        min_length=1,
        description="The raw Google token string (ID token JWT or OAuth access token).",
    )
    client_type: Literal["WEB", "EXTENSION"] = Field(
        default="WEB",
        description="Client category originating the authentication request.",
    )


class UserProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    google_user_id: str
    email: str
    display_name: str | None = None
    picture: str | None = None


class GoogleAuthResponse(BaseModel):
    success: bool = True
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str | None = Field(
        default=None,
        description="Opaque refresh token string, provided in response body for EXTENSION clients. For WEB clients, delivered via HttpOnly cookie.",
    )
    user: UserProfileResponse


class RefreshTokenRequest(BaseModel):
    refresh_token: str | None = Field(
        default=None,
        description="Opaque refresh token string. Required for EXTENSION clients if not using cookie.",
    )


class RefreshTokenResponse(BaseModel):
    success: bool = True
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str | None = Field(
        default=None,
        description="Rotated refresh token, returned in body for EXTENSION clients. For WEB clients, delivered via HttpOnly cookie.",
    )


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(
        default=None,
        description="Optional refresh token to revoke. Required if no Bearer token or cookie is available.",
    )
    all_sessions: bool = Field(
        default=False,
        description="If True, revokes all active sessions for the current user across all devices.",
    )


class LogoutResponse(BaseModel):
    success: bool = True
    message: str = "Đăng xuất thành công, phiên làm việc đã bị thu hồi."

