import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.meeting_dom_segment import MeetingDOMSegment


class MeetingDOMSegmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def bulk_insert_segments(
        self,
        meeting_id: uuid.UUID,
        session_id: uuid.UUID,
        segments: list[dict[str, Any]],
    ) -> list[MeetingDOMSegment]:
        """
        Inserts raw DOM segments with ON CONFLICT (session_id, sequence) DO NOTHING
        to guarantee idempotency and Never Delete Raw invariant.
        """
        if not segments:
            return []

        inserted_segments: list[MeetingDOMSegment] = []
        for seg in segments:
            seg_id = seg.get("segment_id")
            if isinstance(seg_id, str):
                try:
                    seg_id = uuid.UUID(seg_id)
                except ValueError:
                    seg_id = uuid.uuid4()
            elif not isinstance(seg_id, uuid.UUID):
                seg_id = uuid.uuid4()

            stmt = (
                insert(MeetingDOMSegment)
                .values(
                    segment_id=seg_id,
                    meeting_id=meeting_id,
                    session_id=session_id,
                    sequence=seg["sequence"],
                    speaker_name=seg.get("speaker_name") or "Unknown",
                    text=seg["text"],
                    start_time_offset_ms=seg.get("start_time_offset_ms"),
                    end_time_offset_ms=seg.get("end_time_offset_ms"),
                    observed_start_epoch_ms=seg.get("observed_start_epoch_ms") or 0,
                    observed_end_epoch_ms=seg.get("observed_end_epoch_ms") or 0,
                    is_final=seg.get("is_final", True),
                    confidence_score=seg.get("confidence_score", 1.0),
                )
                .on_conflict_do_nothing(
                    index_elements=["session_id", "sequence"]
                )
            )
            self.session.execute(stmt)

        self.session.flush()

        # Fetch and return all segments for this session
        return self.list_by_session_id(session_id)

    def list_by_meeting_id(self, meeting_id: uuid.UUID) -> list[MeetingDOMSegment]:
        return list(
            self.session.scalars(
                select(MeetingDOMSegment)
                .where(MeetingDOMSegment.meeting_id == meeting_id)
                .order_by(MeetingDOMSegment.observed_start_epoch_ms, MeetingDOMSegment.sequence)
            )
        )

    def list_by_session_id(self, session_id: uuid.UUID) -> list[MeetingDOMSegment]:
        return list(
            self.session.scalars(
                select(MeetingDOMSegment)
                .where(MeetingDOMSegment.session_id == session_id)
                .order_by(MeetingDOMSegment.sequence)
            )
        )

    def get_segment_count_by_session(self, session_id: uuid.UUID) -> int:
        return (
            self.session.scalar(
                select(func.count(MeetingDOMSegment.segment_id)).where(
                    MeetingDOMSegment.session_id == session_id
                )
            )
            or 0
        )

    def get_segment_count_by_meeting(self, meeting_id: uuid.UUID) -> int:
        return (
            self.session.scalar(
                select(func.count(MeetingDOMSegment.segment_id)).where(
                    MeetingDOMSegment.meeting_id == meeting_id
                )
            )
            or 0
        )
