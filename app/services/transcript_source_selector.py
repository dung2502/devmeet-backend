import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.services.dom_transcript_normalizer import DomTranscriptNormalizer
from app.services.transcript_normalizer import OfficialTranscriptNormalizer

SourceType = Literal["official", "dom", "none"]


@dataclass(frozen=True)
class TranscriptSourceSelectionResult:
    selected_source: SourceType
    transcript_text: str
    reason: str


def select_transcript_source(
    official_text: str | None,
    dom_text: str | None,
) -> TranscriptSourceSelectionResult:
    """
    Deterministically selects the transcript source according to precedence policy.

    Precedence:
    1. Official Transcript (Source A - Primary Authoritative Source) if non-empty string.
    2. DOM Transcript (Source B - Fallback Source) if Official is missing/empty and DOM is non-empty string.
    3. None if both sources are missing or contain only whitespace/empty strings.
    """
    clean_official = official_text.strip() if official_text else ""
    clean_dom = dom_text.strip() if dom_text else ""

    if clean_official:
        return TranscriptSourceSelectionResult(
            selected_source="official",
            transcript_text=official_text or "",
            reason="Official transcript is available and non-empty (Primary Authoritative Source).",
        )

    if clean_dom:
        return TranscriptSourceSelectionResult(
            selected_source="dom",
            transcript_text=dom_text or "",
            reason="Official transcript is unavailable/empty. DOM transcript selected as fallback.",
        )

    return TranscriptSourceSelectionResult(
        selected_source="none",
        transcript_text="",
        reason="No usable transcript source available.",
    )


class TranscriptSourceSelector:
    """Service to perform source selection for a meeting from PostgreSQL."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.official_normalizer = OfficialTranscriptNormalizer(session)
        self.dom_normalizer = DomTranscriptNormalizer(session)

    def select_source_by_meeting_id(
        self,
        meeting_id: uuid.UUID,
    ) -> TranscriptSourceSelectionResult:
        """
        Orchestrates normalized outputs from Step 3A and Step 3B for a given meeting_id,
        and applies the source selection policy.
        Read-only operation.
        """
        official_text = self.official_normalizer.normalize_by_meeting_id(meeting_id)
        dom_text = self.dom_normalizer.normalize_by_meeting_id(meeting_id)

        return select_transcript_source(
            official_text=official_text,
            dom_text=dom_text,
        )
