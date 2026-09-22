from fastapi import APIRouter, Depends, status

from app.auth import get_current_user
from app.models.user import User
from app.schemas.auth import UserProfileResponse

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/me",
    response_model=UserProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current user profile",
    description="Resolves and returns the authenticated user profile from the Application JWT session.",
)
def get_current_user_profile(
    current_user: User = Depends(get_current_user),
) -> UserProfileResponse:
    return UserProfileResponse.model_validate(current_user)
