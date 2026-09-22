import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import Meeting
from app.services.transcript_normalizer import format_timestamp

UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def format_dom_timestamp(raw_time: Any) -> str:
    """
    Parses various raw timestamp representations into [HH:mm:ss].
    Returns [00:00:00] if raw_time is missing, None, or unparseable.
    """
    if raw_time is None:
        return "[00:00:00]"

    if isinstance(raw_time, datetime):
        return format_timestamp(raw_time)

    if isinstance(raw_time, (int, float)):
        if raw_time > 10_000_000_000:
            total_seconds = int(raw_time / 1000)
        else:
            total_seconds = int(raw_time)
        hours = (total_seconds // 3600) % 24
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"[{hours:02d}:{minutes:02d}:{seconds:02d}]"

    if isinstance(raw_time, str):
        val = raw_time.strip()
        if not val:
            return "[00:00:00]"

        # Case 1: HH:MM:SS or HH:MM:SS.mmm
        if len(val) >= 8 and val[2] == ":" and val[5] == ":":
            hh_mm_ss = val[:8]
            parts = hh_mm_ss.split(":")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                return f"[{hh_mm_ss}]"

        # Case 2: ISO string e.g. "2026-08-21T09:00:15Z"
        try:
            clean_iso = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_iso)
            return f"[{dt.strftime('%H:%M:%S')}]"
        except (ValueError, TypeError):
            pass

    return "[00:00:00]"


def resolve_dom_speaker(entry: dict[str, Any], default_speaker: str = "Unknown") -> str:
    """
    Extracts and normalizes speaker name from a DOM entry dict.
    Prevents technical IDs or UUIDs from leaking as human-readable speaker names.
    """
    speaker = (
        entry.get("speaker")
        or entry.get("display_name")
        or entry.get("speaker_name")
        or entry.get("author")
    )

    if not speaker or not isinstance(speaker, str):
        return default_speaker

    speaker_str = speaker.strip()
    if not speaker_str:
        return default_speaker

    # Reject UUIDs or internal technical IDs
    if UUID_PATTERN.match(speaker_str):
        return default_speaker

    if speaker_str.startswith(("participant_", "user_", "sess-", "sess_")):
        remainder = speaker_str.split("_", 1)[-1]
        if UUID_PATTERN.match(remainder) or (remainder.isalnum() and len(remainder) > 15):
            return default_speaker

    return speaker_str


def resolve_dom_text(entry: dict[str, Any]) -> str:
    """Extracts text content from a DOM entry dict safely."""
    text = entry.get("text")
    if text is None:
        text = entry.get("content")
    if text is None:
        text = entry.get("message")

    if text is None:
        return ""

    return str(text)


def extract_dom_entries(data: Any) -> list[dict[str, Any]]:
    """Extracts list of entry dicts from dom_transcript_data JSONB structure."""
    if not data:
        return []

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if isinstance(data, dict):
        if "sessions" in data and isinstance(data["sessions"], dict):
            entries = []
            seen_ids = set()
            for sess in data["sessions"].values():
                if isinstance(sess, dict) and "segments" in sess and isinstance(sess["segments"], list):
                    for item in sess["segments"]:
                        if isinstance(item, dict):
                            seg_id = item.get("segment_id")
                            if seg_id:
                                if seg_id not in seen_ids:
                                    seen_ids.add(seg_id)
                                    entries.append(item)
                            else:
                                entries.append(item)
            if entries:
                return entries

        if "segments" in data and isinstance(data["segments"], list):
            return [item for item in data["segments"] if isinstance(item, dict)]
        if "entries" in data and isinstance(data["entries"], list):
            return [item for item in data["entries"] if isinstance(item, dict)]
        if "items" in data and isinstance(data["items"], list):
            return [item for item in data["items"] if isinstance(item, dict)]
        if any(
            k in data
            for k in ("speaker", "text", "start_time", "display_name", "content", "captured_at")
        ):
            return [data]

    return []


def get_dom_entry_sort_key(entry: dict[str, Any], index: int) -> tuple[str, int]:
    """
    Returns a deterministic sort key for a DOM transcript entry.
    Primary key: normalized time string [HH:mm:ss]
    Secondary key: original list index to preserve stable order when times match.
    """
    raw_time = (
        entry.get("start_time")
        or entry.get("captured_at")
        or entry.get("observed_start_epoch_ms")
        or entry.get("timestamp")
        or entry.get("time")
        or entry.get("created_at")
    )
    time_str = format_dom_timestamp(raw_time)
    return (time_str, index)


def normalize_dom_transcript_data(
    data: Any,
    default_speaker: str = "Unknown",
) -> str:
    """
    Normalizes dom_transcript_data JSONB structure into official AI-friendly text format.

    Format: [HH:mm:ss] Speaker: Text
    Returns empty string if data is empty or contains no valid entries.
    """
    entries = extract_dom_entries(data)
    if not entries:
        return ""

    indexed_entries = list(enumerate(entries))
    indexed_entries.sort(key=lambda item: get_dom_entry_sort_key(item[1], item[0]))

    lines: list[str] = []
    for _, entry in indexed_entries:
        raw_time = (
            entry.get("start_time")
            or entry.get("captured_at")
            or entry.get("observed_start_epoch_ms")
            or entry.get("timestamp")
            or entry.get("time")
            or entry.get("created_at")
        )
        time_str = format_dom_timestamp(raw_time)
        speaker = resolve_dom_speaker(entry, default_speaker=default_speaker)
        text = resolve_dom_text(entry)

        lines.append(f"{time_str} {speaker}: {text}")

    return "\n".join(lines)


class DomTranscriptNormalizer:
    """Service to load and normalize DOM Transcript data from PostgreSQL Meeting records or meeting_dom_segments."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def normalize_by_meeting_id(
        self,
        meeting_id: uuid.UUID,
        default_speaker: str = "Unknown",
    ) -> str:
        """
        Loads Meeting by meeting_id, extracts raw segments from meeting_dom_segments
        via DOMAggregationService (or fallback to legacy dom_transcript_data),
        and returns normalized text.
        Read-only operation.
        """
        from app.repositories.meeting_dom_segment_repository import MeetingDOMSegmentRepository
        from app.services.dom_aggregation_service import DOMAggregationService

        meeting = self.session.get(Meeting, meeting_id)
        if meeting is None:
            return ""

        dom_repo = MeetingDOMSegmentRepository(self.session)
        seg_count = dom_repo.get_segment_count_by_meeting(meeting_id)

        if seg_count > 0:
            agg_service = DOMAggregationService(self.session)
            base_epoch = int(meeting.start_time.timestamp() * 1000) if meeting.start_time else None
            agg_result = agg_service.compute_aggregated_view(meeting_id, base_epoch_ms=base_epoch)
            lines = [
                f"[{e.timestamp}] {e.speaker}: {e.text}"
                for e in agg_result.entries
                if e.text.strip()
            ]
            return "\n".join(lines)

        if meeting.dom_transcript_data is not None:
            return normalize_dom_transcript_data(
                data=meeting.dom_transcript_data,
                default_speaker=default_speaker,
            )

        return ""

