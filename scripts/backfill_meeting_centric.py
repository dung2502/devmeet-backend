#!/usr/bin/env python3
"""
DevMeeting AI — Meeting-Centric Historical Data Backfill Script (Phase M2)

Idempotent backfill script that migrates existing historical single-user meetings data
into the new Meeting-Centric / Shared Room schema:
1. meeting_access (creates OWNER records for each existing meeting)
2. meetings.conference_identity (populates from conference_record_name or space/URL)
3. meeting_dom_segments (extracts legacy dom_transcript_data segments into raw event store)

Safe to run multiple times with zero data corruption or duplication.
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure backend app is on sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import LiveSession, LiveSessionStatus, Meeting, MeetingAccess, MeetingDOMSegment, MeetingRole
from app.repositories import MeetingAccessRepository, MeetingDOMSegmentRepository


def backfill_meeting_access(db: Session) -> int:
    """Backfills meeting_access table: each existing meeting gets an OWNER entry."""
    meetings = list(db.scalars(select(Meeting)))
    created_count = 0

    access_repo = MeetingAccessRepository(db)
    for m in meetings:
        existing = access_repo.get_access(meeting_id=m.id, user_id=m.user_id)
        if existing is None:
            access_repo.add_or_update_access(
                meeting_id=m.id,
                user_id=m.user_id,
                role=MeetingRole.OWNER.value,
                last_seen_at=m.updated_at,
            )
            created_count += 1

    db.commit()
    return created_count


def backfill_conference_identity(db: Session) -> int:
    """Populates conference_identity on meetings if missing."""
    meetings = list(
        db.scalars(select(Meeting).where(Meeting.conference_identity.is_(None)))
    )
    updated_count = 0

    for m in meetings:
        ident = None
        if m.conference_record_name:
            ident = m.conference_record_name
        elif m.meeting_space_name:
            ident = m.meeting_space_name
        elif m.meeting_url:
            # Extract meeting code from URL (e.g. https://meet.google.com/abc-defg-hij)
            url_part = m.meeting_url.split("/")[-1].split("?")[0].strip()
            if url_part:
                ident = url_part

        if ident:
            m.conference_identity = ident
            updated_count += 1

    db.commit()
    return updated_count


from app.services.dom_transcript_normalizer import extract_dom_entries, resolve_dom_speaker, resolve_dom_text


def backfill_dom_segments(db: Session) -> int:
    """Extracts legacy dom_transcript_data into meeting_dom_segments table."""
    meetings = list(
        db.scalars(
            select(Meeting).where(Meeting.dom_transcript_data.is_not(None))
        )
    )
    total_segments_inserted = 0
    dom_repo = MeetingDOMSegmentRepository(db)

    for m in meetings:
        data = m.dom_transcript_data
        if not isinstance(data, dict):
            continue

        raw_segments = extract_dom_entries(data)
        if not raw_segments:
            continue

        # Find or create a LiveSession for this meeting
        live_session = db.scalar(
            select(LiveSession).where(
                LiveSession.meeting_id == m.id,
                LiveSession.user_id == m.user_id,
            )
        )
        if live_session is None:
            live_session = LiveSession(
                session_id=uuid.uuid4(),
                meeting_id=m.id,
                user_id=m.user_id,
                tab_session_uuid=uuid.uuid4(),
                status=LiveSessionStatus.COMPLETED.value,
            )
            db.add(live_session)
            db.commit()
            db.refresh(live_session)

        # Base timestamp calculation
        base_epoch_ms = (
            int(m.start_time.timestamp() * 1000)
            if m.start_time
            else int(m.created_at.timestamp() * 1000)
        )

        segments_to_insert = []
        for idx, seg in enumerate(raw_segments, start=1):
            seq = seg.get("sequence") or idx
            start_offset = seg.get("start_time_offset_ms") or 0
            end_offset = seg.get("end_time_offset_ms") or (start_offset + 2000)

            observed_start = seg.get("observed_start_epoch_ms") or (base_epoch_ms + start_offset)
            observed_end = seg.get("observed_end_epoch_ms") or (base_epoch_ms + end_offset)

            seg_dict = {
                "segment_id": seg.get("segment_id") or uuid.uuid4(),
                "sequence": seq,
                "speaker_name": seg.get("speaker_name") or seg.get("speaker") or "Unknown Speaker",
                "text": seg.get("text", "").strip(),
                "start_time_offset_ms": start_offset,
                "end_time_offset_ms": end_offset,
                "observed_start_epoch_ms": observed_start,
                "observed_end_epoch_ms": observed_end,
                "is_final": seg.get("is_final", True),
                "confidence_score": float(seg.get("confidence_score", 1.0)),
            }
            if seg_dict["text"]:
                segments_to_insert.append(seg_dict)


        if segments_to_insert:
            inserted = dom_repo.bulk_insert_segments(
                meeting_id=m.id,
                session_id=live_session.session_id,
                segments=segments_to_insert,
            )
            total_segments_inserted += len(inserted)

    db.commit()
    return total_segments_inserted


def run_backfill(db: Session | None = None) -> dict[str, int]:
    """Main execution function for Phase M2 backfill."""
    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True

    try:
        print("[M2 Backfill] Starting idempotent backfill...")
        access_count = backfill_meeting_access(db)
        print(f"[M2 Backfill] Created {access_count} meeting_access records.")

        identity_count = backfill_conference_identity(db)
        print(f"[M2 Backfill] Updated {identity_count} meeting conference_identity values.")

        segments_count = backfill_dom_segments(db)
        print(f"[M2 Backfill] Ingested {segments_count} raw DOM segments into meeting_dom_segments.")

        print("[M2 Backfill] Backfill completed successfully!")
        return {
            "access_records_created": access_count,
            "conference_identities_updated": identity_count,
            "dom_segments_ingested": segments_count,
        }
    finally:
        if should_close:
            db.close()


if __name__ == "__main__":
    run_backfill()
