import uuid
from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Participant, Transcript, TranscriptEntry


def format_timestamp(dt: datetime | None) -> str:
    """Format a datetime object to [HH:mm:ss]. Returns [00:00:00] if dt is None."""
    if dt is None:
        return "[00:00:00]"
    return f"[{dt.strftime('%H:%M:%S')}]"


def normalize_official_transcript_entries(
    entries: Sequence[TranscriptEntry],
    participants_map: dict[uuid.UUID, str] | None = None,
    default_speaker: str = "Unknown",
) -> str:
    """
    Normalizes a sequence of Official TranscriptEntry objects into AI-friendly text format.

    Format: [HH:mm:ss] Display Name: Text
    Entries are sorted deterministically by start_time ASC.
    """
    if not entries:
        return ""

    # Helper key for sorting start_time safely when tz-aware and tz-naive datetime values might be mixed
    def get_sort_key(entry: TranscriptEntry) -> tuple[datetime, datetime, str]:
        st = entry.start_time
        if st is None:
            st = datetime.min
        # Remove tzinfo for sorting comparison if needed
        st_naive = st.replace(tzinfo=None) if st.tzinfo is not None else st

        ca = getattr(entry, "created_at", None)
        if ca is None:
            ca = datetime.min
        ca_naive = ca.replace(tzinfo=None) if ca.tzinfo is not None else ca

        return (st_naive, ca_naive, str(entry.id))

    sorted_entries = sorted(entries, key=get_sort_key)

    lines: list[str] = []
    for entry in sorted_entries:
        speaker: str | None = None

        if entry.participant_id and participants_map:
            speaker = participants_map.get(entry.participant_id)

        if not speaker and entry.participant and entry.participant.display_name:
            speaker = entry.participant.display_name

        if not speaker or not speaker.strip():
            speaker = default_speaker

        time_str = format_timestamp(entry.start_time)
        text_content = entry.text if entry.text is not None else ""

        line = f"{time_str} {speaker}: {text_content}"
        lines.append(line)

    return "\n".join(lines)


class OfficialTranscriptNormalizer:
    """Service to load and normalize Official Transcript entries from PostgreSQL."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def normalize_by_meeting_id(
        self,
        meeting_id: uuid.UUID,
        default_speaker: str = "Unknown",
    ) -> str:
        """
        Loads all Official Transcript entries for a given meeting_id,
        resolves participant display names, and returns normalized text.
        """
        participants = list(
            self.session.scalars(
                select(Participant).where(Participant.meeting_id == meeting_id)
            )
        )
        participants_map = {
            p.id: p.display_name
            for p in participants
            if p.display_name is not None and p.display_name.strip() != ""
        }

        entries = list(
            self.session.scalars(
                select(TranscriptEntry)
                .join(Transcript)
                .where(Transcript.meeting_id == meeting_id)
            )
        )

        return normalize_official_transcript_entries(
            entries=entries,
            participants_map=participants_map,
            default_speaker=default_speaker,
        )

    def normalize_by_transcript_id(
        self,
        transcript_id: uuid.UUID,
        default_speaker: str = "Unknown",
    ) -> str:
        """
        Loads all Official Transcript entries for a given transcript_id,
        resolves participant display names, and returns normalized text.
        """
        transcript = self.session.get(Transcript, transcript_id)
        participants_map: dict[uuid.UUID, str] = {}

        if transcript is not None and transcript.meeting_id is not None:
            participants = list(
                self.session.scalars(
                    select(Participant).where(
                        Participant.meeting_id == transcript.meeting_id
                    )
                )
            )
            participants_map = {
                p.id: p.display_name
                for p in participants
                if p.display_name is not None and p.display_name.strip() != ""
            }

        entries = list(
            self.session.scalars(
                select(TranscriptEntry).where(
                    TranscriptEntry.transcript_id == transcript_id
                )
            )
        )

        return normalize_official_transcript_entries(
            entries=entries,
            participants_map=participants_map,
            default_speaker=default_speaker,
        )
