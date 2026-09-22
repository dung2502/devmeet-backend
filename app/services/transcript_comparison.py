import re
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy.orm import Session

from app.models import Meeting
from app.services.dom_transcript_normalizer import DomTranscriptNormalizer
from app.services.transcript_normalizer import OfficialTranscriptNormalizer

LINE_REGEX = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*([^:]+):\s*(.*)$")


@dataclass(frozen=True)
class TranscriptComparisonMetrics:
    coverage_rate: float
    missing_count: int
    duplicate_count: int
    speaker_match_rate: float
    timestamp_delta_avg_sec: float


@dataclass(frozen=True)
class TranscriptComparisonResult:
    meeting_id: uuid.UUID | None
    comparison_status: str  # "completed", "skipped", "pending"
    metrics: TranscriptComparisonMetrics | None
    reason: str


@dataclass(frozen=True)
class ParsedTranscriptLine:
    time_sec: int
    speaker: str
    text: str


def parse_normalized_line(line: str) -> ParsedTranscriptLine | None:
    """Parses a normalized line '[HH:mm:ss] Speaker: Text' into ParsedTranscriptLine."""
    clean_line = line.strip()
    if not clean_line:
        return None

    match = LINE_REGEX.match(clean_line)
    if match:
        h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3))
        time_sec = h * 3600 + m * 60 + s
        speaker = match.group(4).strip()
        text = match.group(5)
        return ParsedTranscriptLine(time_sec=time_sec, speaker=speaker, text=text)

    # Fallback if bracket timestamp is missing
    if ":" in clean_line:
        parts = clean_line.split(":", 1)
        return ParsedTranscriptLine(time_sec=0, speaker=parts[0].strip(), text=parts[1].strip())

    return ParsedTranscriptLine(time_sec=0, speaker="Unknown", text=clean_line)


def compare_normalized_transcripts(
    official_text: str | None,
    dom_text: str | None,
    meeting_id: uuid.UUID | None = None,
) -> TranscriptComparisonResult:
    """
    Deterministically compares Normalized Official Transcript against Normalized DOM Transcript.

    Computes:
    - coverage_rate: ratio of official entries covered by DOM transcript (0.0 to 1.0)
    - missing_count: number of official entries missing from DOM transcript
    - duplicate_count: number of duplicate entries detected in DOM transcript
    - speaker_match_rate: ratio of matching speaker names for aligned entries (0.0 to 1.0)
    - timestamp_delta_avg_sec: average timestamp difference in seconds between matched entries
    """
    clean_official = official_text.strip() if official_text else ""
    clean_dom = dom_text.strip() if dom_text else ""

    if not clean_official and not clean_dom:
        return TranscriptComparisonResult(
            meeting_id=meeting_id,
            comparison_status="skipped",
            metrics=None,
            reason="Both official and DOM transcripts are empty or missing.",
        )

    if not clean_official or not clean_dom:
        return TranscriptComparisonResult(
            meeting_id=meeting_id,
            comparison_status="skipped",
            metrics=None,
            reason="Comparison skipped: One of the transcript sources is missing or empty.",
        )

    # Parse lines
    off_lines = [
        p for p in (parse_normalized_line(l) for l in clean_official.split("\n")) if p is not None
    ]
    dom_lines = [
        p for p in (parse_normalized_line(l) for l in clean_dom.split("\n")) if p is not None
    ]

    if not off_lines or not dom_lines:
        return TranscriptComparisonResult(
            meeting_id=meeting_id,
            comparison_status="skipped",
            metrics=None,
            reason="Comparison skipped: Unable to parse lines from one or both transcripts.",
        )

    # Detect duplicates in DOM transcript
    seen_dom_entries: set[tuple[str, str]] = set()
    duplicate_count = 0
    for dl in dom_lines:
        key = (dl.speaker.lower(), dl.text.strip().lower())
        if key in seen_dom_entries:
            duplicate_count += 1
        else:
            seen_dom_entries.add(key)

    # Alignment and matching
    matched_dom_indices: set[int] = set()
    matched_count = 0
    speaker_matches = 0
    timestamp_deltas: list[float] = []

    for ol in off_lines:
        ol_text_clean = ol.text.strip().lower()
        best_match_idx: int | None = None
        best_match_score = 0.0

        for idx, dl in enumerate(dom_lines):
            if idx in matched_dom_indices:
                continue

            dl_text_clean = dl.text.strip().lower()
            if ol_text_clean == dl_text_clean:
                best_match_idx = idx
                best_match_score = 1.0
                break

            # Calculate string similarity ratio for fuzzy matching
            ratio = SequenceMatcher(None, ol_text_clean, dl_text_clean).ratio()
            if ratio >= 0.70 and ratio > best_match_score:
                best_match_score = ratio
                best_match_idx = idx

        if best_match_idx is not None:
            matched_dom_indices.add(best_match_idx)
            matched_count += 1
            matched_dom = dom_lines[best_match_idx]

            # Timestamp delta
            delta = abs(ol.time_sec - matched_dom.time_sec)
            timestamp_deltas.append(float(delta))

            # Speaker match check
            if ol.speaker.strip().lower() == matched_dom.speaker.strip().lower():
                speaker_matches += 1

    missing_count = max(0, len(off_lines) - matched_count)
    coverage_rate = round(matched_count / float(len(off_lines)), 3) if off_lines else 1.0
    coverage_rate = min(1.0, max(0.0, coverage_rate))

    speaker_match_rate = round(speaker_matches / float(matched_count), 3) if matched_count > 0 else 1.0
    speaker_match_rate = min(1.0, max(0.0, speaker_match_rate))

    timestamp_delta_avg = (
        round(sum(timestamp_deltas) / float(len(timestamp_deltas)), 2)
        if timestamp_deltas
        else 0.0
    )

    metrics = TranscriptComparisonMetrics(
        coverage_rate=coverage_rate,
        missing_count=missing_count,
        duplicate_count=duplicate_count,
        speaker_match_rate=speaker_match_rate,
        timestamp_delta_avg_sec=timestamp_delta_avg,
    )

    return TranscriptComparisonResult(
        meeting_id=meeting_id,
        comparison_status="completed",
        metrics=metrics,
        reason="Transcript quality comparison completed successfully.",
    )


class TranscriptComparisonEngine:
    """Service to load transcript sources from PostgreSQL and compute quality comparison metrics."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.official_normalizer = OfficialTranscriptNormalizer(session)
        self.dom_normalizer = DomTranscriptNormalizer(session)

    def compare_by_meeting_id(
        self,
        meeting_id: uuid.UUID,
        persist: bool = False,
    ) -> TranscriptComparisonResult:
        """
        Loads official and DOM normalized transcripts for meeting_id,
        computes comparison metrics, and optionally persists results into Meeting record.
        """
        official_text = self.official_normalizer.normalize_by_meeting_id(meeting_id)
        dom_text = self.dom_normalizer.normalize_by_meeting_id(meeting_id)

        result = compare_normalized_transcripts(
            official_text=official_text,
            dom_text=dom_text,
            meeting_id=meeting_id,
        )

        if persist:
            meeting = self.session.get(Meeting, meeting_id)
            if meeting is not None:
                meeting.comparison_status = result.comparison_status
                if result.metrics is not None:
                    meeting.comparison_metrics = {
                        "coverage_rate": result.metrics.coverage_rate,
                        "missing_count": result.metrics.missing_count,
                        "duplicate_count": result.metrics.duplicate_count,
                        "speaker_match_rate": result.metrics.speaker_match_rate,
                        "timestamp_delta_avg_sec": result.metrics.timestamp_delta_avg_sec,
                    }
                self.session.commit()

        return result
