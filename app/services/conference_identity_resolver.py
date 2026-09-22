import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Meeting, MeetingAccess, MeetingRole, User
from app.repositories import MeetingAccessRepository, MeetingRepository


class ConferenceIdentityResolver:
    """
    Multi-Tier Conference Identity Resolution Service.
    Resolves client sync requests to a single canonical Shared Room meeting_id:
    - Tier 1: Strong Identity (conference_record_name match)
    - Tier 2: Contextual Identity (meeting_space_name / meeting_code in active window)
    - Tier 3: Create new Shared Room

    Ensures calling user is registered in meeting_access (OWNER or PARTICIPANT).
    Guarded with PostgreSQL Advisory Lock and Partial Unique Index.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.meeting_repo = MeetingRepository(session)
        self.access_repo = MeetingAccessRepository(session)

    def extract_meeting_code(self, meeting_url: str | None, meeting_space_name: str | None) -> str | None:
        if meeting_space_name and meeting_space_name.strip():
            # e.g. "spaces/abc-defg-hij" or "abc-defg-hij"
            return meeting_space_name.split("/")[-1].strip()
        if meeting_url and meeting_url.strip():
            # e.g. "https://meet.google.com/abc-defg-hij"
            return meeting_url.split("/")[-1].split("?")[0].strip()
        return None

    def normalize_meeting_title(self, title: str | None, meeting_code: str | None) -> str:
        """
        Normalizes meeting title to standard canonical format:
        - If title is generic ("Meet", "Google Meet", "Google Meet Session", empty) -> "Meet - <meeting_code>"
        - If title is legacy "Google Meet (<meeting_code>)" -> "Meet - <meeting_code>"
        - If title is a custom calendar/user title (e.g. "Sprint Planning") -> "Sprint Planning"
        """
        if not title or not title.strip():
            return f"Meet - {meeting_code.strip()}" if meeting_code else "Meet"

        stripped = title.strip()
        lower = stripped.lower()

        # Generic title checks
        if lower in ("meet", "google meet", "google meet session", "google meet (undefined)"):
            return f"Meet - {meeting_code.strip()}" if meeting_code else "Meet"

        # Legacy format: "Google Meet (xxx-yyyy-zzz)" -> "Meet - xxx-yyyy-zzz"
        if lower.startswith("google meet (") and lower.endswith(")"):
            inner = stripped[12:-1].strip()
            if inner:
                return f"Meet - {inner}"
            return f"Meet - {meeting_code.strip()}" if meeting_code else "Meet"

        return stripped

    def resolve_or_create_shared_meeting(
        self,
        user_id: uuid.UUID,
        conference_record_name: str | None = None,
        meeting_space_name: str | None = None,
        meeting_url: str | None = None,
        title: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        meeting_id: uuid.UUID | None = None,
    ) -> tuple[Meeting, bool]:
        """
        Resolves or creates a shared meeting under a PostgreSQL transaction advisory lock.
        Returns (meeting, is_created).
        """
        now = datetime.now(timezone.utc)
        meeting_code = self.extract_meeting_code(meeting_url, meeting_space_name)
        resolved_title = self.normalize_meeting_title(title, meeting_code)
        conference_ident = conference_record_name or meeting_code or (meeting_space_name.split("/")[-1] if meeting_space_name else None)

        # 1. Acquire PostgreSQL Advisory Lock on conference_ident to serialize concurrent room creation
        if conference_ident:
            try:
                self.session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:conf_id))"),
                    {"conf_id": conference_ident},
                )
            except Exception:
                # If DB does not support advisory lock (e.g. SQLite / mock tests), continue gracefully
                pass

        # Case 0: Direct meeting_id provided by client extension context
        if meeting_id is not None:
            existing = self.meeting_repo.get_by_id(meeting_id)
            if existing is not None:
                update_vals: dict[str, Any] = {}
                existing_lower = (existing.title or "").strip().lower()
                if (existing_lower in ("meet", "google meet", "google meet session") or not existing.title) and resolved_title != "Meet":
                    update_vals["title"] = resolved_title
                elif title and title != existing.title and title.strip().lower() not in ("meet", "google meet"):
                    update_vals["title"] = resolved_title
                if meeting_url:
                    update_vals["meeting_url"] = meeting_url
                if meeting_space_name:
                    update_vals["meeting_space_name"] = meeting_space_name
                if conference_record_name:
                    update_vals["conference_record_name"] = conference_record_name
                if update_vals:
                    existing = self.meeting_repo.update(existing.id, update_vals) or existing

                user_role = MeetingRole.OWNER.value if existing.user_id == user_id else MeetingRole.PARTICIPANT.value
                self.access_repo.add_or_update_access(
                    meeting_id=existing.id,
                    user_id=user_id,
                    role=user_role,
                    last_seen_at=now,
                )
                self.session.commit()
                return existing, False

        # Tier 1 & 2: Search active in_progress meeting matching conference_ident / code
        from app.models.live_session import LiveSession

        lookup_terms = [t for t in [conference_ident, meeting_code, conference_record_name, meeting_space_name] if t]
        active_meeting = None
        if lookup_terms:
            stmt = (
                select(Meeting)
                .where(
                    (Meeting.conference_identity.in_(lookup_terms))
                    | (Meeting.meeting_space_name.in_(lookup_terms))
                    | (Meeting.conference_record_name.in_(lookup_terms))
                    | (Meeting.meeting_url.ilike(f"%{meeting_code}%") if meeting_code else False)
                )
                .where(Meeting.status == "in_progress")
                .order_by(Meeting.created_at.desc())
            )
            active_meeting = self.session.scalar(stmt)

        if active_meeting is not None:
            # Check Continuity Window (30 minutes)
            max_session_hb = self.session.scalar(
                select(func.max(LiveSession.last_heartbeat_at))
                .where(LiveSession.meeting_id == active_meeting.id)
            )
            latest_activity = max(
                filter(None, [max_session_hb, active_meeting.last_heartbeat_at, active_meeting.created_at]),
                default=active_meeting.created_at,
            )
            if latest_activity.tzinfo is None:
                latest_activity = latest_activity.replace(tzinfo=timezone.utc)

            is_within_window = (now - latest_activity).total_seconds() <= 1800  # 30 minutes

            if is_within_window:
                # Valid active shared room -> Join as PARTICIPANT (or keep OWNER if creator)
                user_role = MeetingRole.OWNER.value if active_meeting.user_id == user_id else MeetingRole.PARTICIPANT.value
                self.access_repo.add_or_update_access(
                    meeting_id=active_meeting.id,
                    user_id=user_id,
                    role=user_role,
                    last_seen_at=now,
                )

                update_vals = {}
                active_lower = (active_meeting.title or "").strip().lower()
                if (active_lower in ("meet", "google meet", "google meet session") or not active_meeting.title) and resolved_title != "Meet":
                    update_vals["title"] = resolved_title
                if conference_record_name and not active_meeting.conference_record_name:
                    update_vals["conference_record_name"] = conference_record_name
                if update_vals:
                    self.meeting_repo.update(active_meeting.id, update_vals)

                self.session.commit()
                return active_meeting, False
            else:
                # Expired Continuity Window (> 30 mins inactive):
                # Close stale in_progress room to 'completed' BEFORE creating new room
                # to satisfy Partial Unique Index uq_active_conference_identity
                active_meeting.status = "completed"
                active_meeting.updated_at = now
                self.session.flush()

        # Tier 2.5: Check recently completed meeting within Grace Period (180s)
        # Allows tab reload or brief disconnection to resume same room without fragmenting
        if active_meeting is None and lookup_terms:
            recent_completed_stmt = (
                select(Meeting)
                .where(
                    (Meeting.conference_identity.in_(lookup_terms))
                    | (Meeting.meeting_space_name.in_(lookup_terms))
                    | (Meeting.conference_record_name.in_(lookup_terms))
                    | (Meeting.meeting_url.ilike(f"%{meeting_code}%") if meeting_code else False)
                )
                .where(Meeting.status == "completed")
                .order_by(Meeting.created_at.desc())
            )
            candidate_completed = self.session.scalar(recent_completed_stmt)
            if candidate_completed is not None:
                # Chốt chặn AI: chỉ phục hồi nếu chưa xử lý AI
                ai_safe = candidate_completed.ai_status in ("NOT_PROCESSED", None)
                if ai_safe:
                    max_session_hb = self.session.scalar(
                        select(func.max(LiveSession.last_heartbeat_at))
                        .where(LiveSession.meeting_id == candidate_completed.id)
                    )
                    # Refinement 1: Require active heartbeat or explicit end_time to determine grace period.
                    # Do not rely on updated_at/created_at to avoid false revivals of completed meetings.
                    latest_time = max(
                        filter(None, [
                            candidate_completed.end_time,
                            max_session_hb,
                            candidate_completed.last_heartbeat_at,
                        ]),
                        default=None,
                    )
                    if latest_time is not None:
                        if latest_time.tzinfo is None:
                            latest_time = latest_time.replace(tzinfo=timezone.utc)

                        if (now - latest_time).total_seconds() <= 180:
                            # Phục hồi phòng: reset status sang 'in_progress' và end_time = None
                            candidate_completed.status = "in_progress"
                            candidate_completed.end_time = None
                            candidate_completed.last_heartbeat_at = now
                            candidate_completed.updated_at = now

                            user_role = MeetingRole.OWNER.value if candidate_completed.user_id == user_id else MeetingRole.PARTICIPANT.value
                            self.access_repo.add_or_update_access(
                                meeting_id=candidate_completed.id,
                                user_id=user_id,
                                role=user_role,
                                last_seen_at=now,
                            )
                            self.session.commit()
                            return candidate_completed, False

        # Tier 3: Create new Shared Room
        values = {
            "user_id": user_id,
            "conference_record_name": conference_record_name,
            "meeting_space_name": meeting_space_name,
            "meeting_url": meeting_url,
            "title": resolved_title,
            "start_time": start_time or now,
            "end_time": end_time,
            "status": "in_progress",
            "conference_identity": conference_ident,
            "last_heartbeat_at": now,
        }

        try:
            new_meeting = self.meeting_repo.create(values)
            self.access_repo.add_or_update_access(
                meeting_id=new_meeting.id,
                user_id=user_id,
                role=MeetingRole.OWNER.value,
                last_seen_at=now,
            )
            self.session.commit()
            return new_meeting, True
        except IntegrityError:
            self.session.rollback()
            # Concurrent insertion occurred; retry finding the active room
            if meeting_code:
                existing_retry = self.meeting_repo.find_active_by_meeting_code(meeting_code)
                if existing_retry:
                    user_role = MeetingRole.OWNER.value if existing_retry.user_id == user_id else MeetingRole.PARTICIPANT.value
                    self.access_repo.add_or_update_access(
                        meeting_id=existing_retry.id,
                        user_id=user_id,
                        role=user_role,
                        last_seen_at=now,
                    )
                    self.session.commit()
                    return existing_retry, False
            raise
