import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.api.v1.error_handlers import LiveSessionNotFoundError, SessionPayloadConflictError
from app.models.live_session import LiveSessionStatus
from app.repositories.live_session_repository import LiveSessionRepository
from app.repositories.meeting_dom_segment_repository import MeetingDOMSegmentRepository
from app.repositories.meeting_repository import MeetingRepository
from app.schemas.live_session import FinalizeTranscriptRequest, FinalizeTranscriptResponse
from app.utils.hashing import compute_transcript_content_hash

logger = logging.getLogger(__name__)


class FinalizeService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.live_session_repo = LiveSessionRepository(session)
        self.meeting_repo = MeetingRepository(session)
        self.dom_segment_repo = MeetingDOMSegmentRepository(session)

    def finalize_transcript(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        payload: FinalizeTranscriptRequest,
    ) -> tuple[FinalizeTranscriptResponse, bool]:
        """
        Persist finalized DOM transcript for a live session with dual-write support:
        1. Primary: Raw immutable segments stored in meeting_dom_segments
        2. Legacy Dual-Write: Aggregated JSONB stored in meetings.dom_transcript_data
        3. Sets live_sessions.status = 'COMPLETED'

        Returns (response, is_created) where is_created=True means 201, False means 200.
        """
        # 1. Ownership check - fail closed
        live_session = self.live_session_repo.get_by_id_and_user(session_id, user_id)
        if live_session is None:
            raise LiveSessionNotFoundError(str(session_id))

        # 2. Get associated meeting
        meeting = self.meeting_repo.get_by_id(live_session.meeting_id)
        if meeting is None:
            raise LiveSessionNotFoundError(str(session_id))

        # 3. Compute content hash of incoming segments
        raw_segments: list[dict[str, Any]] = [s.model_dump() for s in payload.segments]
        incoming_hash = compute_transcript_content_hash(raw_segments)
        str_session_id = str(session_id)

        # 4. Session-level idempotency check
        existing_data = meeting.dom_transcript_data
        session_records: dict[str, Any] = {}

        if isinstance(existing_data, dict):
            if "sessions" in existing_data and isinstance(existing_data["sessions"], dict):
                session_records = dict(existing_data["sessions"])
            elif "session_id" in existing_data:
                prev_sid = str(existing_data["session_id"])
                session_records[prev_sid] = {
                    "content_hash": existing_data.get("content_hash", ""),
                    "segments": existing_data.get("segments", []),
                    "segment_count": existing_data.get("segment_count", 0),
                    "captured_at": existing_data.get("captured_at", ""),
                }

            if str_session_id in session_records:
                stored_session = session_records[str_session_id]
                stored_hash = stored_session.get("content_hash", "")
                if stored_hash == incoming_hash:
                    logger.info("Finalize idempotent: session_id=%s hash=%s", session_id, incoming_hash)
                    seg_count = self.dom_segment_repo.get_segment_count_by_session(session_id)
                    return (
                        FinalizeTranscriptResponse(
                            session_id=session_id,
                            meeting_id=live_session.meeting_id,
                            dom_capture_status=meeting.dom_capture_status,
                            status="accepted",
                            raw_segments_ingested=seg_count,
                        ),
                        False,
                    )
                else:
                    logger.warning(
                        "Finalize conflict: session_id=%s stored_hash=%s incoming_hash=%s",
                        session_id,
                        stored_hash,
                        incoming_hash,
                    )
                    raise SessionPayloadConflictError(str_session_id)

        # 5. Dual-Write Step 1: Ingest into meeting_dom_segments (Primary Canonical Raw Store)
        base_epoch_ms = (
            int(meeting.start_time.timestamp() * 1000)
            if meeting.start_time
            else int(meeting.created_at.timestamp() * 1000)
        )
        clock_offset = live_session.client_server_offset_ms or 0

        segments_to_insert: list[dict[str, Any]] = []
        for idx, seg in enumerate(payload.segments, start=1):
            seq = seg.sequence or idx
            start_off = seg.start_time_offset_ms
            end_off = seg.end_time_offset_ms or ((start_off + 2000) if start_off is not None else None)

            obs_start = seg.observed_start_epoch_ms or (base_epoch_ms + (start_off or 0) + clock_offset)
            obs_end = seg.observed_end_epoch_ms or (base_epoch_ms + (end_off or 2000) + clock_offset)

            seg_id = None
            if seg.segment_id:
                try:
                    seg_id = uuid.UUID(seg.segment_id)
                except ValueError:
                    seg_id = uuid.uuid4()
            else:
                seg_id = uuid.uuid4()

            segments_to_insert.append({
                "segment_id": seg_id,
                "sequence": seq,
                "speaker_name": seg.speaker_name or "Unknown Speaker",
                "text": seg.text.strip(),
                "start_time_offset_ms": start_off,
                "end_time_offset_ms": end_off,
                "observed_start_epoch_ms": obs_start,
                "observed_end_epoch_ms": obs_end,
                "is_final": True,
                "confidence_score": seg.confidence_score or 1.0,
            })

        inserted_segments = self.dom_segment_repo.bulk_insert_segments(
            meeting_id=live_session.meeting_id,
            session_id=session_id,
            segments=segments_to_insert,
        )

        # Ingest unique DOM speakers into participants table
        from app.repositories.participant_repository import ParticipantRepository
        part_repo = ParticipantRepository(self.session)
        meeting = self.meeting_repo.get_by_id(live_session.meeting_id)
        unique_speakers = {
            s.speaker_name.strip()
            for s in payload.segments
            if s.speaker_name and s.speaker_name.strip() not in ("", "Unknown", "Unknown Speaker")
        }
        for spk in unique_speakers:
            try:
                user_email = (
                    meeting.user.email
                    if meeting and meeting.user and meeting.user.display_name == spk
                    else None
                )
                part_repo.upsert({
                    "meeting_id": live_session.meeting_id,
                    "google_participant_name": f"dom_{spk}",
                    "display_name": spk,
                    "email": user_email,
                    "participant_type": "signed_in_user",
                })
            except Exception as exc:
                logger.warning("Failed to upsert participant for speaker %s: %s", spk, exc)

        # 6. Dual-Write Step 2: Ingest into legacy dom_transcript_data
        session_payload_data = {
            "session_id": str_session_id,
            "meeting_id": str(live_session.meeting_id),
            "segments": [s.model_dump() for s in payload.segments],
            "segment_count": payload.segment_count,
            "captured_at": payload.captured_at,
            "content_hash": incoming_hash,
        }
        session_records[str_session_id] = session_payload_data

        dom_data: dict[str, Any] = {
            **session_payload_data,
            "sessions": session_records,
        }

        updated = self.meeting_repo.update(
            live_session.meeting_id,
            {"dom_transcript_data": dom_data, "dom_capture_status": "received"},
        )

        # 7. Update live_session status to COMPLETED
        live_session.status = LiveSessionStatus.COMPLETED.value
        self.session.commit()
        self.session.refresh(live_session)

        logger.info(
            "Finalize accepted: session_id=%s meeting_id=%s segments=%d raw_ingested=%d hash=%s",
            session_id,
            live_session.meeting_id,
            payload.segment_count,
            len(inserted_segments),
            incoming_hash,
        )
        return (
            FinalizeTranscriptResponse(
                session_id=session_id,
                meeting_id=live_session.meeting_id,
                dom_capture_status=updated.dom_capture_status if updated else "received",
                status="accepted",
                raw_segments_ingested=len(inserted_segments),
            ),
            True,
        )
