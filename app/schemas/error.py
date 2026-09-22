from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Any | None = None
    request_id: str | None = None
    meeting_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
