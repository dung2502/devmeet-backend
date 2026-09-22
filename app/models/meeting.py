import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.live_session import LiveSession
    from app.models.meeting_access import MeetingAccess
    from app.models.meeting_dom_segment import MeetingDOMSegment
    from app.models.participant import Participant
    from app.models.transcript import Transcript
    from app.models.user import User


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        Index("idx_meetings_user_id", "user_id"),
        Index("idx_meetings_start_time", "start_time"),
        Index("idx_meetings_conference_identity", "conference_identity"),
        Index("idx_meetings_identity_status", "conference_identity", "status"),
        Index(
            "uq_active_conference_identity",
            "conference_identity",
            unique=True,
            postgresql_where=text("status = 'in_progress' AND conference_identity IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    conference_record_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )
    meeting_space_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meeting_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="in_progress",
    )
    transcript_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="not_available",
    )
    ai_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="NOT_PROCESSED",
    )
    ai_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sheets_sync_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="NOT_SYNCED",
    )
    dom_capture_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="not_captured",
    )
    dom_transcript_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    comparison_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="pending",
    )
    comparison_metrics: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    conference_identity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    grace_period_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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

    user: Mapped["User"] = relationship(back_populates="meetings")
    participants: Mapped[list["Participant"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
    )
    transcripts: Mapped[list["Transcript"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
    )
    live_sessions: Mapped[list["LiveSession"]] = relationship(back_populates="meeting", cascade="all, delete-orphan")
    access_records: Mapped[list["MeetingAccess"]] = relationship(back_populates="meeting", cascade="all, delete-orphan")
    dom_segments: Mapped[list["MeetingDOMSegment"]] = relationship(back_populates="meeting", cascade="all, delete-orphan")

    @property
    def meeting_code(self) -> str | None:
        if self.meeting_space_name and "/" in self.meeting_space_name:
            return self.meeting_space_name.split("/")[-1].strip()
        if self.meeting_space_name:
            return self.meeting_space_name.strip()
        if self.meeting_url:
            code = self.meeting_url.split("?")[0].rstrip("/").split("/")[-1].strip()
            if code and len(code) >= 3 and "-" in code:
                return code
        if self.conference_identity and "-" in self.conference_identity:
            return self.conference_identity
        return None


