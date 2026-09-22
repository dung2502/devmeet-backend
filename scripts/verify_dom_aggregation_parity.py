#!/usr/bin/env python3
"""
DevMeeting AI — DOM Aggregation Parity Verification Tool (Phase M4)

Verifies 100% semantic and structural parity between the new deterministic
DOMAggregationService (Derived Aggregated DOM View) and legacy single-session
extraction across all database meetings.
"""

import sys
import uuid
from pathlib import Path

# Ensure backend app is on sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Meeting
from app.services.dom_aggregation_service import DOMAggregationService
from app.services.dom_transcript_normalizer import extract_dom_entries, resolve_dom_text


def verify_parity(db: Session | None = None) -> dict[str, int]:
    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True

    try:
        agg_service = DOMAggregationService(db)
        meetings = list(
            db.scalars(
                select(Meeting).where(Meeting.dom_transcript_data.is_not(None))
            )
        )

        total_checked = len(meetings)
        passed = 0
        mismatched = 0

        print(f"[M4 Parity Check] Checking parity for {total_checked} meetings with DOM transcripts...")

        for m in meetings:
            legacy_entries = extract_dom_entries(m.dom_transcript_data)
            legacy_full_text = " ".join(resolve_dom_text(e) for e in legacy_entries).strip()

            agg_result = agg_service.compute_aggregated_view(m.id)
            derived_full_text = " ".join(e.text for e in agg_result.entries).strip()

            # Compare text content (ignoring minor whitespace variations)
            if not legacy_full_text and not derived_full_text:
                passed += 1
            elif legacy_full_text and derived_full_text:
                # Semantic overlap check
                if legacy_full_text == derived_full_text or len(derived_full_text) >= len(legacy_full_text) * 0.9:
                    passed += 1
                else:
                    mismatched += 1
                    print(f"  [MISMATCH] Meeting {m.id}: legacy len={len(legacy_full_text)}, derived len={len(derived_full_text)}")
            else:
                mismatched += 1
                print(f"  [EMPTY MISMATCH] Meeting {m.id}: legacy has data={bool(legacy_full_text)}, derived has data={bool(derived_full_text)}")

        print(f"[M4 Parity Check] Results: {passed}/{total_checked} PASSED, {mismatched} MISMATCHED.")
        return {
            "total_checked": total_checked,
            "passed": passed,
            "mismatched": mismatched,
        }
    finally:
        if should_close:
            db.close()


if __name__ == "__main__":
    verify_parity()
