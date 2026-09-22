import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.meeting import Meeting
    from app.models.meeting_dom_segment import MeetingDOMSegment
    from app.models.user import User


class LiveSessionStatus(str, enum.Enum):
    INIT = "INIT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DISCONNECTED = "DISCONNECTED"
    COMPLETED = "COMPLETED"
    TIMED_OUT = "TIMED_OUT"


class LiveSession(Base):
    __tablename__ = "live_sessions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "meeting_id",
            "tab_session_uuid",
            name="uq_live_sessions_user_meeting_tab",
        ),
        Index("idx_live_sessions_meeting_id", "meeting_id"),
        Index("idx_live_sessions_user_id", "user_id"),
        Index(
            "uq_live_sessions_active_user_meeting",
            "user_id",
            "meeting_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
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
    tab_session_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=LiveSessionStatus.ACTIVE.value,
        server_default=LiveSessionStatus.ACTIVE.value,
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    client_server_offset_ms: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    meeting: Mapped["Meeting"] = relationship(back_populates="live_sessions")
    user: Mapped["User"] = relationship(back_populates="live_sessions")
    dom_segments: Mapped[list["MeetingDOMSegment"]] = relationship(back_populates="live_session", cascade="none", passive_deletes=False)

