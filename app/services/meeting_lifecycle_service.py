import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Meeting
from app.models.live_session import LiveSession, LiveSessionStatus
from app.repositories.meeting_repository import MeetingRepository

logger = logging.getLogger(__name__)


class MeetingLifecycleService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.meeting_repo = MeetingRepository(session)

    def reap_timed_out_sessions(self, ttl_seconds: int = 120) -> int:
        """
        Marks active sessions that haven't sent a heartbeat within ttl_seconds as TIMED_OUT.
        Returns the number of reaped sessions.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=ttl_seconds)

        stmt = (
            update(LiveSession)
            .where(
                LiveSession.status == LiveSessionStatus.ACTIVE.value,
                LiveSession.last_heartbeat_at.is_not(None),
                LiveSession.last_heartbeat_at < cutoff,
            )
            .values(
                status=LiveSessionStatus.TIMED_OUT.value,
                updated_at=now,
            )
        )
        res = self.session.execute(stmt)
        self.session.commit()
        return res.rowcount

    def reap_orphaned_meetings(self, session_ttl_seconds: int = 120, min_age_seconds: int = 180) -> int:
        """
        Finds in_progress meetings that have no active sessions within session_ttl_seconds
        and were created at least min_age_seconds ago, and transitions them to completed.
        Returns the number of completed meetings.
        """
        now = datetime.now(timezone.utc)
        active_cutoff = now - timedelta(seconds=session_ttl_seconds)
        age_cutoff = now - timedelta(seconds=min_age_seconds)

        active_subq = (
            select(LiveSession.session_id)
            .where(
                LiveSession.meeting_id == Meeting.id,
                LiveSession.status == LiveSessionStatus.ACTIVE.value,
                LiveSession.last_heartbeat_at >= active_cutoff,
            )
        )

        stmt = (
            select(Meeting)
            .where(
                Meeting.status == "in_progress",
                Meeting.created_at < age_cutoff,
                ~active_subq.exists(),
            )
        )

        orphaned = list(self.session.scalars(stmt))
        completed_count = 0

        for m in orphaned:
            # If grace_period_expires_at was set and hasn't expired yet, honor it
            if m.grace_period_expires_at and m.grace_period_expires_at > now:
                continue

            m.status = "completed"
            if m.end_time is None:
                m.end_time = now
            if m.transcript_status == "not_available":
                m.transcript_status = "processing"
            m.updated_at = now
            completed_count += 1

        if completed_count > 0:
            self.session.commit()

        return completed_count

    def reap_stale_ai_processing(self, timeout_minutes: int = 5) -> int:
        """
        Resets meetings stuck in ai_status == 'PROCESSING' for more than timeout_minutes to 'FAILED'
        to avoid permanent AI deadlock.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=timeout_minutes)

        stmt = (
            update(Meeting)
            .where(
                Meeting.ai_status == "PROCESSING",
                Meeting.updated_at < cutoff,
            )
            .values(
                ai_status="FAILED",
                updated_at=now,
            )
        )
        res = self.session.execute(stmt)
        self.session.commit()
        return res.rowcount
