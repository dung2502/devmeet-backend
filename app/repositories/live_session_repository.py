import uuid
from typing import Any
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Meeting
from app.models.live_session import LiveSession, LiveSessionStatus


class LiveSessionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, session_id: uuid.UUID) -> LiveSession | None:
        return self.session.get(LiveSession, session_id)

    def list_by_meeting_id(self, meeting_id: uuid.UUID) -> list[LiveSession]:
        return list(
            self.session.scalars(
                select(LiveSession)
                .where(LiveSession.meeting_id == meeting_id)
                .order_by(LiveSession.created_at)
            )
        )

    def list_by_user_id(self, user_id: uuid.UUID) -> list[LiveSession]:
        return list(
            self.session.scalars(
                select(LiveSession)
                .where(LiveSession.user_id == user_id)
                .order_by(LiveSession.created_at)
            )
        )

    def create_or_get_or_supersede_session(
        self,
        meeting_id: uuid.UUID,
        user_id: uuid.UUID,
        tab_session_uuid: uuid.UUID,
    ) -> tuple[LiveSession, bool]:
        """
        Atomically creates a new active session, returns existing idempotent session,
        or supersedes existing active session for the same user and meeting.

        Lock order: Meeting (FOR UPDATE) -> LiveSessions

        Returns (session, is_created) where is_created is True if a new row was inserted.
        """
        try:
            from app.services.participant_sync import MeetingNotFoundError

            # 1. Lock Meeting row
            meeting = self.session.scalar(
                select(Meeting).where(Meeting.id == meeting_id).with_for_update()
            )
            if meeting is None:
                raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

            # 2. Check same idempotency tuple: (user_id, meeting_id, tab_session_uuid)
            existing_session = self.session.scalar(
                select(LiveSession).where(
                    LiveSession.user_id == user_id,
                    LiveSession.meeting_id == meeting_id,
                    LiveSession.tab_session_uuid == tab_session_uuid,
                )
            )
            if existing_session is not None:
                return existing_session, False

            # 3. Supersede active session(s) for same (user_id, meeting_id)
            active_sessions = list(
                self.session.scalars(
                    select(LiveSession)
                    .where(
                        LiveSession.user_id == user_id,
                        LiveSession.meeting_id == meeting_id,
                        LiveSession.status == LiveSessionStatus.ACTIVE.value,
                    )
                    .with_for_update()
                )
            )
            for old_session in active_sessions:
                old_session.status = LiveSessionStatus.DISCONNECTED.value

            # 4. Insert new session
            new_session = LiveSession(
                session_id=uuid.uuid4(),
                meeting_id=meeting_id,
                user_id=user_id,
                tab_session_uuid=tab_session_uuid,
                status=LiveSessionStatus.ACTIVE.value,
            )
            self.session.add(new_session)
            self.session.commit()
            self.session.refresh(new_session)
            return new_session, True

        except IntegrityError:
            self.session.rollback()
            # Check if created by concurrent request with same idempotency tuple
            existing_session = self.session.scalar(
                select(LiveSession).where(
                    LiveSession.user_id == user_id,
                    LiveSession.meeting_id == meeting_id,
                    LiveSession.tab_session_uuid == tab_session_uuid,
                )
            )
            if existing_session is not None:
                return existing_session, False
            # Retry creation once if concurrent race occurred on active supersede
            try:
                meeting = self.session.scalar(
                    select(Meeting).where(Meeting.id == meeting_id).with_for_update()
                )
                if meeting is None:
                    raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

                active_sessions = list(
                    self.session.scalars(
                        select(LiveSession)
                        .where(
                            LiveSession.user_id == user_id,
                            LiveSession.meeting_id == meeting_id,
                            LiveSession.status == LiveSessionStatus.ACTIVE.value,
                        )
                        .with_for_update()
                    )
                )
                for old_session in active_sessions:
                    old_session.status = LiveSessionStatus.DISCONNECTED.value

                new_session = LiveSession(
                    session_id=uuid.uuid4(),
                    meeting_id=meeting_id,
                    user_id=user_id,
                    tab_session_uuid=tab_session_uuid,
                    status=LiveSessionStatus.ACTIVE.value,
                )
                self.session.add(new_session)
                self.session.commit()
                self.session.refresh(new_session)
                return new_session, True
            except Exception:
                self.session.rollback()
                raise

    def get_by_id_and_user(self, session_id: uuid.UUID, user_id: uuid.UUID) -> "LiveSession | None":
        """
        Return the LiveSession only if it belongs to the given user.
        Returns None if the session does not exist OR is owned by a different user.
        Fail-closed ownership check for Phase 5D /finalize.
        """
        return self.session.scalar(
            select(LiveSession).where(
                LiveSession.session_id == session_id,
                LiveSession.user_id == user_id,
            )
        )

    def update_heartbeat(
        self,
        session_id: uuid.UUID,
        client_timestamp_ms: int,
        server_timestamp_ms: int,
    ) -> tuple[LiveSession | None, int]:
        """
        Updates session heartbeat timestamp and calculates client-server clock offset.
        offset = server_time - client_time.
        """
        session = self.get_by_id(session_id)
        if session is None:
            return None, 0

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        offset_ms = server_timestamp_ms - client_timestamp_ms

        session.last_heartbeat_at = now
        session.client_server_offset_ms = offset_ms
        if session.status != LiveSessionStatus.ACTIVE.value:
            session.status = LiveSessionStatus.ACTIVE.value

        # Throttle meeting's last_heartbeat_at update to avoid row lock contention (max once / 120s)
        if session.meeting:
            meeting_hb = session.meeting.last_heartbeat_at
            if meeting_hb is None or (now - meeting_hb).total_seconds() >= 120:
                session.meeting.last_heartbeat_at = now

        self.session.commit()
        self.session.refresh(session)
        return session, offset_ms

    def get_active_sessions_for_meeting(
        self,
        meeting_id: uuid.UUID,
        ttl_seconds: int = 90,
    ) -> list[LiveSession]:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        return list(
            self.session.scalars(
                select(LiveSession)
                .where(
                    LiveSession.meeting_id == meeting_id,
                    LiveSession.status == LiveSessionStatus.ACTIVE.value,
                    (LiveSession.last_heartbeat_at >= cutoff) | (LiveSession.last_heartbeat_at.is_(None)),
                )
            )
        )

    def count_active_sessions_for_meeting(
        self,
        meeting_id: uuid.UUID,
        ttl_seconds: int = 90,
    ) -> int:
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import func
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        return (
            self.session.scalar(
                select(func.count(LiveSession.session_id))
                .where(
                    LiveSession.meeting_id == meeting_id,
                    LiveSession.status == LiveSessionStatus.ACTIVE.value,
                    (LiveSession.last_heartbeat_at >= cutoff) | (LiveSession.last_heartbeat_at.is_(None)),
                )
            )
            or 0
        )

    def mark_user_sessions_completed(
        self,
        meeting_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> int:
        from sqlalchemy import update
        stmt = (
            update(LiveSession)
            .where(
                LiveSession.meeting_id == meeting_id,
                LiveSession.user_id == user_id,
                LiveSession.status == LiveSessionStatus.ACTIVE.value,
            )
            .values(
                status=LiveSessionStatus.COMPLETED.value,
            )
        )
        res = self.session.execute(stmt)
        self.session.commit()
        return res.rowcount

