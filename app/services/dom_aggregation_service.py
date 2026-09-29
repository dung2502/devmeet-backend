import difflib
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.meeting_dom_segment import MeetingDOMSegment
from app.repositories.live_session_repository import LiveSessionRepository
from app.repositories.meeting_dom_segment_repository import MeetingDOMSegmentRepository
from app.schemas.transcript_view import TranscriptViewEntry


def strip_committed_prefix(current_text: str, committed_text: str | None) -> str:
    """
    Strips already committed / aggregated prefix text from an accumulating caption.
    Used for MS Teams & Zoom Subtitle Bar accumulation resilience in Derived Clean Views.

    Guarantees:
    1. Word-boundary exact prefix match: ensures 'Alonso' is not stripped by 'Alo'.
    2. Word-boundary normalized token match: tolerates STT punctuation/casing shifts.
    3. Math.ceil threshold: requires 100% word match for short phrases (<= 4 words)
       to avoid stripping independent sentences sharing common initial words.
    4. Substring containment: returns '' if current_text is fully contained in committed_text.
    """
    if not committed_text or not committed_text.strip():
        return current_text.strip()

    curr = current_text.strip()
    committed = committed_text.strip()
    if not curr:
        return ""
    if curr == committed:
        return ""

    # 1. Direct exact prefix match with word boundary guard
    if curr.startswith(committed):
        if len(curr) == len(committed):
            return ""
        next_char = curr[len(committed)]
        # Must be followed by whitespace or punctuation
        if re.match(r"[\s\W]", next_char):
            return curr[len(committed):].strip()

    # 2. Word-boundary normalized token match
    def normalize_token(w: str) -> str:
        return re.sub(r"^[^\w]+|[^\w]+$", "", w.lower(), flags=re.UNICODE)

    committed_words = [w for w in committed.split() if w]
    curr_words = [w for w in curr.split() if w]

    if committed_words and len(curr_words) >= len(committed_words):
        def is_word_match(w1: str, w2: str) -> bool:
            if w1 == w2:
                return True
            if abs(len(w1) - len(w2)) <= 1 and difflib.SequenceMatcher(None, w1, w2).ratio() >= 0.8:
                return True
            return False

        match_count = 0
        for i in range(len(committed_words)):
            cw = normalize_token(committed_words[i])
            rw = normalize_token(curr_words[i])
            if cw and rw and is_word_match(cw, rw):
                match_count += 1
            else:
                break

        # Require >= 80% match using math.ceil (guarantees 100% match for 1-4 words)
        required_matches = math.ceil(len(committed_words) * 0.8)
        if match_count >= required_matches and match_count > 0:
            remaining_words = curr_words[match_count:]
            return " ".join(remaining_words).strip()

    # 3. Reverse check: if current text is fully contained inside committed text (retraction / duplicate)
    if curr in committed:
        return ""

    return curr


@dataclass
class AggregatedDOMViewResult:
    meeting_id: uuid.UUID
    entries: list[TranscriptViewEntry] = field(default_factory=list)
    total_raw_segments: int = 0
    total_aggregated_entries: int = 0
    cross_session_coverage: float = 0.0
    session_contributors: list[str] = field(default_factory=list)


class DOMAggregationService:
    """
    Deterministic Multi-Session DOM Captions Aggregation Service.
    Transforms raw immutable client observations into a clean, deduplicated,
    chronologically aligned Derived Aggregated DOM View.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.dom_repo = MeetingDOMSegmentRepository(session)
        self.live_session_repo = LiveSessionRepository(session)

    def _format_timestamp(self, epoch_ms: int, base_epoch_ms: int | None = None) -> str:
        """Formats relative offset or absolute wall clock into HH:MM:SS format."""
        if base_epoch_ms and epoch_ms >= base_epoch_ms:
            offset_sec = max(0, int((epoch_ms - base_epoch_ms) / 1000))
        else:
            offset_sec = int(epoch_ms / 1000) % 86400

        hours = offset_sec // 3600
        minutes = (offset_sec % 3600) // 60
        seconds = offset_sec % 60
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _text_similarity(self, a: str, b: str) -> float:
        """Computes text similarity ratio between two utterance strings."""
        if not a or not b:
            return 0.0
        a_clean = a.strip().lower()
        b_clean = b.strip().lower()
        if a_clean == b_clean or a_clean in b_clean or b_clean in a_clean:
            return 1.0
        return difflib.SequenceMatcher(None, a_clean, b_clean).ratio()

    def _calculate_coverage(
        self,
        intervals: list[tuple[int, int]],
        total_span_ms: int,
    ) -> float:
        """Calculates percentage coverage across merged non-overlapping time intervals."""
        if not intervals or total_span_ms <= 0:
            return 100.0 if intervals else 0.0

        # Sort and merge intervals
        sorted_intervals = sorted(intervals, key=lambda x: x[0])
        merged: list[list[int]] = []
        for start, end in sorted_intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, max(start + 1000, end)])
            else:
                merged[-1][1] = max(merged[-1][1], end)

        captured_duration_ms = sum(end - start for start, end in merged)
        coverage_pct = round(min(100.0, (captured_duration_ms / max(total_span_ms, 1)) * 100.0), 1)
        return coverage_pct

    def compute_aggregated_view(
        self,
        meeting_id: uuid.UUID,
        base_epoch_ms: int | None = None,
    ) -> AggregatedDOMViewResult:
        """
        Deterministically computes the Derived Aggregated DOM View from raw observations.
        """
        raw_segments = self.dom_repo.list_by_meeting_id(meeting_id)
        if not raw_segments:
            return AggregatedDOMViewResult(meeting_id=meeting_id)

        # 1. Fetch live session clock offsets
        contributor_sessions = {str(seg.session_id) for seg in raw_segments}
        offsets: dict[uuid.UUID, int] = {}
        for sid_str in contributor_sessions:
            try:
                sid = uuid.UUID(sid_str)
                sess = self.live_session_repo.get_by_id(sid)
                offsets[sid] = sess.client_server_offset_ms if sess else 0
            except ValueError:
                pass

        # 2. Calibrate and sort observations
        calibrated_items = []
        intervals: list[tuple[int, int]] = []
        for seg in raw_segments:
            offset = offsets.get(seg.session_id, 0)
            cal_start = max(0, seg.observed_start_epoch_ms - offset)
            cal_end = max(cal_start + 1000, seg.observed_end_epoch_ms - offset)

            calibrated_items.append({
                "segment_id": seg.segment_id,
                "session_id": str(seg.session_id),
                "sequence": seg.sequence,
                "speaker": seg.speaker_name or "Unknown Speaker",
                "text": seg.text.strip(),
                "calibrated_start": cal_start,
                "calibrated_end": cal_end,
                "confidence": seg.confidence_score or 1.0,
            })
            intervals.append((cal_start, cal_end))

        # Sort globally by calibrated start time, then sequence
        calibrated_items.sort(key=lambda x: (x["calibrated_start"], x["sequence"]))

        # Base epoch reference
        min_epoch = calibrated_items[0]["calibrated_start"]
        max_epoch = max(item["calibrated_end"] for item in calibrated_items)
        reference_base = base_epoch_ms if base_epoch_ms is not None else min_epoch
        total_span = max(1000, max_epoch - min_epoch)

        # 3. Multi-Session Deduplication & Speaker Grouping
        aggregated_utterances: list[dict[str, Any]] = []

        # Keep track of the latest cumulative raw DOM text seen per (session_id, speaker)
        # to cleanly strip carryover prefixes in accumulating platforms (MS Teams / Zoom Subtitle Bar).
        session_cumulative_raw: dict[tuple[str, str], str] = {}

        for item in calibrated_items:
            key = (item["session_id"], item["speaker"])
            prev_raw = session_cumulative_raw.get(key)
            item_raw_text = item["text"]

            if not aggregated_utterances:
                aggregated_utterances.append(dict(item))
                session_cumulative_raw[key] = item_raw_text
                continue

            last = aggregated_utterances[-1]
            time_delta_ms = abs(item["calibrated_start"] - last["calibrated_start"])

            # Check if this item is a cross-session duplicate
            if item["session_id"] != last["session_id"]:
                similarity = self._text_similarity(item["text"], last["text"])
                if time_delta_ms <= 2500 and similarity >= 0.70:
                    # Duplicate utterance from another participant's caption observer
                    # Prefer longer / higher confidence utterance
                    if len(item["text"]) > len(last["text"]) or item["confidence"] > last["confidence"]:
                        last["text"] = item["text"]
                        last["confidence"] = max(last["confidence"], item["confidence"])
                        last["calibrated_end"] = max(last["calibrated_end"], item["calibrated_end"])
                    session_cumulative_raw[key] = item_raw_text
                    continue

            # Check if same speaker speaking consecutively
            is_same_speaker = item["speaker"] == last["speaker"]
            is_same_session = item["session_id"] == last["session_id"]
            gap_ms = item["calibrated_start"] - last["calibrated_end"]

            if is_same_speaker and is_same_session:
                delta = strip_committed_prefix(item_raw_text, prev_raw) if prev_raw else item_raw_text
                session_cumulative_raw[key] = item_raw_text

                if gap_ms <= 3000:
                    # Consecutive speech within 3.0s pause threshold
                    is_superset = (
                        item_raw_text.startswith(last["text"]) or
                        (prev_raw and delta != item_raw_text and len(item_raw_text) > len(last["text"]))
                    )

                    if is_superset:
                        # If last utterance matches prev_raw prefix, replace with progressive raw text
                        if prev_raw and (last["text"] == prev_raw or last["text"].startswith(prev_raw)):
                            last["text"] = item_raw_text
                        elif delta and delta not in last["text"]:
                            last["text"] = f"{last['text']} {delta}".strip()
                        elif item_raw_text not in last["text"] and not delta:
                            pass
                        else:
                            last["text"] = item_raw_text
                        last["calibrated_end"] = max(last["calibrated_end"], item["calibrated_end"])
                    elif item_raw_text in last["text"] or not delta:
                        # Exact duplicate or retraction; ignore duplicate text
                        last["calibrated_end"] = max(last["calibrated_end"], item["calibrated_end"])
                    else:
                        # Distinct new sentence within 3s: append delta/sentence
                        append_text = delta if delta else item_raw_text
                        if append_text not in last["text"]:
                            last["text"] = f"{last['text']} {append_text}".strip()
                        last["calibrated_end"] = max(last["calibrated_end"], item["calibrated_end"])
                else:
                    # Pause > 3.0s: Separate utterance
                    # If this is an accumulating container that carried over previous words,
                    # only use the fresh delta text (if >= 4 words were stripped).
                    if delta and delta != item_raw_text:
                        prev_word_count = len(prev_raw.split()) if prev_raw else 0
                        if prev_word_count >= 4:
                            item_to_add = dict(item)
                            item_to_add["text"] = delta
                            aggregated_utterances.append(item_to_add)
                        else:
                            aggregated_utterances.append(dict(item))
                    elif delta:
                        aggregated_utterances.append(dict(item))
            elif is_same_speaker and not is_same_session and gap_ms <= 3000:
                session_cumulative_raw[key] = item_raw_text
                if item["text"] not in last["text"]:
                    last["text"] = f"{last['text']} {item['text']}".strip()
                last["calibrated_end"] = max(last["calibrated_end"], item["calibrated_end"])
            else:
                session_cumulative_raw[key] = item_raw_text
                aggregated_utterances.append(dict(item))

        # 4. Format Output View Entries
        view_entries: list[TranscriptViewEntry] = []
        for utt in aggregated_utterances:
            timestamp_str = self._format_timestamp(utt["calibrated_start"], reference_base)
            view_entries.append(
                TranscriptViewEntry(
                    speaker=utt["speaker"],
                    timestamp=timestamp_str,
                    text=utt["text"],
                )
            )

        coverage_pct = self._calculate_coverage(intervals, total_span)

        return AggregatedDOMViewResult(
            meeting_id=meeting_id,
            entries=view_entries,
            total_raw_segments=len(raw_segments),
            total_aggregated_entries=len(view_entries),
            cross_session_coverage=coverage_pct,
            session_contributors=sorted(list(contributor_sessions)),
        )
