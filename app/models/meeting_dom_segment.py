import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.live_session import LiveSession
    from app.models.meeting import Meeting


class MeetingDOMSegment(Base):
    __tablename__ = "meeting_dom_segments"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_meeting_dom_segments_session_sequence"),
        Index("idx_meeting_dom_segments_meeting_epoch", "meeting_id", "observed_start_epoch_ms"),
        Index("idx_meeting_dom_segments_session_id", "session_id"),
    )

    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("live_sessions.session_id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    speaker_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    start_time_offset_ms: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    end_time_offset_ms: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    observed_start_epoch_ms: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    observed_end_epoch_ms: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    is_final: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    confidence_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default="1.0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    meeting: Mapped["Meeting"] = relationship(back_populates="dom_segments")
    live_session: Mapped["LiveSession"] = relationship(back_populates="dom_segments")
