import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.meeting import Meeting
    from app.models.user import User


class MeetingRole(str, enum.Enum):
    OWNER = "OWNER"
    PARTICIPANT = "PARTICIPANT"
    VIEWER = "VIEWER"


class MeetingAccess(Base):
    __tablename__ = "meeting_access"
    __table_args__ = (
        UniqueConstraint("meeting_id", "user_id", name="uq_meeting_access_meeting_user"),
        Index("idx_meeting_access_meeting_id", "meeting_id"),
        Index("idx_meeting_access_user_meeting", "user_id", "meeting_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=MeetingRole.PARTICIPANT.value,
        server_default=MeetingRole.PARTICIPANT.value,
    )
    first_joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    meeting: Mapped["Meeting"] = relationship(back_populates="access_records")
    user: Mapped["User"] = relationship(back_populates="meeting_access_records")
