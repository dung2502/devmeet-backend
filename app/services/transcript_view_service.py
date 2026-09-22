import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Meeting, Participant, Transcript, TranscriptEntry
from app.repositories.live_session_repository import LiveSessionRepository
from app.repositories.meeting_access_repository import MeetingAccessRepository
from app.repositories.meeting_dom_segment_repository import MeetingDOMSegmentRepository
from app.schemas.transcript_view import (
    SourceTypeEnum,
    TranscriptViewEntry,
    TranscriptViewResponse,
)
from app.services.dom_aggregation_service import DOMAggregationService
from app.services.dom_transcript_normalizer import (
    extract_dom_entries,
    format_dom_timestamp,
    get_dom_entry_sort_key,
    resolve_dom_speaker,
    resolve_dom_text,
)
from app.services.participant_sync import MeetingNotFoundError
from app.services.transcript_normalizer import format_timestamp
from app.services.transcript_source_selector import TranscriptSourceSelector


class TranscriptViewService:
    """Service to load and present normalized Clean Transcript entries according to Hybrid Source Selection rules."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.selector = TranscriptSourceSelector(session)
        self.access_repo = MeetingAccessRepository(session)
        self.live_session_repo = LiveSessionRepository(session)
        self.dom_segment_repo = MeetingDOMSegmentRepository(session)
        self.aggregation_service = DOMAggregationService(session)

    def get_transcript_view(
        self,
        meeting_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> TranscriptViewResponse:
        meeting = self.session.get(Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

        if user_id is not None:
            user_role = self.access_repo.get_user_role(meeting_id, user_id)
            if meeting.user_id != user_id and user_role is None:
                raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

        # 1. Deterministic Phase 3 Source Selection
        selection_result = self.selector.select_source_by_meeting_id(meeting_id)
        selected_source = selection_result.selected_source

        active_sessions = self.live_session_repo.count_active_sessions_for_meeting(meeting_id, ttl_seconds=90)

        entries: list[TranscriptViewEntry] = []

        if selected_source == "official":
            # Load and resolve Official entries
            participants = list(
                self.session.scalars(
                    select(Participant).where(Participant.meeting_id == meeting_id)
                )
            )
            part_map = {
                p.id: p.display_name
                for p in participants
                if p.display_name and p.display_name.strip()
            }

            raw_entries = list(
                self.session.scalars(
                    select(TranscriptEntry)
                    .join(Transcript)
                    .where(Transcript.meeting_id == meeting_id)
                )
            )

            def get_sort_key(entry: TranscriptEntry):
                st = entry.start_time
                st_naive = st.replace(tzinfo=None) if st and st.tzinfo else st
                ca = getattr(entry, "created_at", None)
                ca_naive = ca.replace(tzinfo=None) if ca and ca.tzinfo else ca
                return (st_naive, ca_naive, str(entry.id))

            sorted_entries = sorted(raw_entries, key=get_sort_key)

            for e in sorted_entries:
                speaker = part_map.get(e.participant_id) if e.participant_id else None
                if not speaker and e.participant and e.participant.display_name:
                    speaker = e.participant.display_name
                if not speaker or not speaker.strip():
                    speaker = "Unknown"

                time_str = format_timestamp(e.start_time).strip("[]")
                entries.append(
                    TranscriptViewEntry(
                        speaker=speaker,
                        timestamp=time_str,
                        text=e.text or "",
                    )
                )

            return TranscriptViewResponse(
                meeting_id=meeting.id,
                source="OFFICIAL",
                total_entries=len(entries),
                reason=selection_result.reason,
                entries=entries,
                cross_session_coverage=100.0 if entries else 0.0,
                active_capture_sessions=active_sessions,
            )

        if selected_source == "dom":
            # Check if we have raw segments in meeting_dom_segments
            raw_count = self.dom_segment_repo.get_segment_count_by_meeting(meeting_id)
            if raw_count > 0:
                base_epoch = int(meeting.start_time.timestamp() * 1000) if meeting.start_time else None
                agg_result = self.aggregation_service.compute_aggregated_view(
                    meeting_id=meeting_id,
                    base_epoch_ms=base_epoch,
                )
                return TranscriptViewResponse(
                    meeting_id=meeting.id,
                    source="DOM",
                    total_entries=agg_result.total_aggregated_entries,
                    reason=selection_result.reason,
                    entries=agg_result.entries,
                    cross_session_coverage=agg_result.cross_session_coverage,
                    session_contributors=agg_result.session_contributors,
                    active_capture_sessions=active_sessions,
                )

            # Fallback for historical meetings: load legacy dom_transcript_data
            dom_data = meeting.dom_transcript_data
            raw_dom_entries = extract_dom_entries(dom_data)
            indexed_entries = list(enumerate(raw_dom_entries))
            indexed_entries.sort(key=lambda item: get_dom_entry_sort_key(item[1], item[0]))

            for _, d_entry in indexed_entries:
                raw_time = (
                    d_entry.get("start_time")
                    or d_entry.get("captured_at")
                    or d_entry.get("observed_start_epoch_ms")
                    or d_entry.get("timestamp")
                    or d_entry.get("time")
                    or d_entry.get("created_at")
                )
                time_str = format_dom_timestamp(raw_time).strip("[]")
                speaker = resolve_dom_speaker(d_entry, default_speaker="Unknown")
                text = resolve_dom_text(d_entry)

                entries.append(
                    TranscriptViewEntry(
                        speaker=speaker,
                        timestamp=time_str,
                        text=text,
                    )
                )

            return TranscriptViewResponse(
                meeting_id=meeting.id,
                source="DOM",
                total_entries=len(entries),
                reason=selection_result.reason,
                entries=entries,
                cross_session_coverage=100.0 if entries else 0.0,
                active_capture_sessions=active_sessions,
            )

        return TranscriptViewResponse(
            meeting_id=meeting.id,
            source="NONE",
            total_entries=0,
            reason=selection_result.reason,
            entries=[],
            active_capture_sessions=active_sessions,
        )
