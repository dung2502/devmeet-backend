"""
Canonical JSON hash utility for Phase 5D transcript idempotency.

Hash is computed over the CONTENT of transcript segments only.
Canonical contract: sequence + speaker_name + text.
Excluded from hash: segment_id, captured_at, speaker_avatar_url, status, source.
"""
import hashlib
import json
from typing import Any


def _canonical_segment(segment: dict[str, Any]) -> dict[str, Any]:
    """
    Extract only stable, content-bearing fields from a segment dict.
    Excludes: segment_id, captured_at, speaker_avatar_url, status, source.
    """
    return {
        "sequence": int(segment.get("sequence", 0)),
        "speaker_name": segment.get("speaker_name") or None,
        "text": str(segment.get("text", "")),
    }


def compute_transcript_content_hash(segments: list[dict[str, Any]]) -> str:
    """
    Compute a deterministic SHA-256 hash over finalized transcript segment content.

    Algorithm:
    1. Sort segments by `sequence` (ascending).
    2. For each segment, extract only (sequence, speaker_name, text).
    3. Serialize to canonical JSON (sort_keys=True, ensure_ascii=False, separators=(",", ":")).
    4. Hash with SHA-256.
    5. Return "sha256:<hex_digest>".

    This hash is stable across:
    - Different segment_id values (generated UUIDs)
    - Different captured_at timestamps
    - Different speaker_avatar_url values
    - Multiple identical uploads (idempotency)
    """
    sorted_segments = sorted(segments, key=lambda s: int(s.get("sequence", 0)))
    canonical = [_canonical_segment(s) for s in sorted_segments]
    canonical_json = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"

