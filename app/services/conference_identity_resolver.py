import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Meeting, MeetingAccess, MeetingRole, User
from app.repositories import MeetingAccessRepository, MeetingRepository


class ConferenceIdentityResolver:
    """
    Multi-Tier Conference Identity Resolution Service supporting Multi-Platform (Google Meet & Zoom Web).
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

    def sanitize_meeting_url(self, meeting_url: str | None, platform: str) -> str | None:
        """
        Strips sensitive tokens (e.g. pwd query param in Zoom URLs) before persistence.
        """
        if not meeting_url or not meeting_url.strip():
            return None
        url = meeting_url.strip()
        if platform == "ZOOM_WEB" or "zoom.us" in url.lower() or "zoomgov.com" in url.lower():
            try:
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                qs.pop("pwd", None)
                clean_query = urlencode(qs, doseq=True)
                return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, clean_query, parsed.fragment))
            except Exception:
                return url
        return url

    def extract_meeting_code(
        self,
        meeting_url: str | None,
        meeting_space_name: str | None,
        platform: str = "GOOGLE_MEET",
    ) -> str | None:
        if platform == "ZOOM_WEB" or (meeting_url and ("zoom.us" in meeting_url.lower() or "zoomgov.com" in meeting_url.lower())):
            if meeting_url:
                # Matches: /wc/1234567890/join, /j/1234567890, /wc/join/1234567890
                m = re.search(r"/(?:wc|j)(?:/join)?/(\d{9,11})", meeting_url)
                if m:
                    return m.group(1)
                m_conf = re.search(r"[?&]confno=(\d{9,11})", meeting_url)
                if m_conf:
                    return m_conf.group(1)
            if meeting_space_name and meeting_space_name.strip():
                clean_space = re.sub(r"\D", "", meeting_space_name)
                if 9 <= len(clean_space) <= 11:
                    return clean_space
                clean_slug = re.sub(r"[^a-zA-Z0-9_\-]", "", meeting_space_name.strip())
                if clean_slug:
                    return clean_slug
            return None

        # Google Meet logic
        if meeting_space_name and meeting_space_name.strip():
            # e.g. "spaces/abc-defg-hij" or "abc-defg-hij"
            return meeting_space_name.split("/")[-1].strip()
        if meeting_url and meeting_url.strip():
            # e.g. "https://meet.google.com/abc-defg-hij"
            return meeting_url.split("/")[-1].split("?")[0].strip()
        return None

    def normalize_meeting_title(
        self,
        title: str | None,
        meeting_code: str | None,
        platform: str = "GOOGLE_MEET",
    ) -> str:
        """
        Normalizes meeting title to standard canonical format:
        - If title is generic ("Meet", "Google Meet", "Zoom", "Zoom Meeting", empty) -> "<Prefix> - <meeting_code>"
        - If title is legacy "Google Meet (<meeting_code>)" -> "Meet - <meeting_code>"
        - If title is a custom calendar/user title (e.g. "Sprint Planning") -> "Sprint Planning"
        """
        default_prefix = "Zoom" if platform == "ZOOM_WEB" else "Meet"
        if not title or not title.strip():
            return f"{default_prefix} - {meeting_code.strip()}" if meeting_code else default_prefix

        stripped = title.strip()
        lower = stripped.lower()

        generic_titles = (
            "meet", "google meet", "google meet session", "google meet (undefined)",
            "zoom", "zoom meeting", "zoom session", "zoom web client", "zoom (undefined)",
        )
        if lower in generic_titles:
            return f"{default_prefix} - {meeting_code.strip()}" if meeting_code else default_prefix

        # Legacy format: "Google Meet (xxx-yyyy-zzz)" -> "Meet - xxx-yyyy-zzz"
        if lower.startswith("google meet (") and lower.endswith(")"):
            inner = stripped[12:-1].strip()
            return f"Meet - {inner}" if inner else (f"Meet - {meeting_code.strip()}" if meeting_code else "Meet")

        if lower.startswith("zoom meeting (") and lower.endswith(")"):
            inner = stripped[14:-1].strip()
            return f"Zoom - {inner}" if inner else (f"Zoom - {meeting_code.strip()}" if meeting_code else "Zoom")

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
        platform: str = "GOOGLE_MEET",
        meeting_code: str | None = None,
    ) -> tuple[Meeting, bool]:
        """
        Resolves or creates a shared meeting under a PostgreSQL transaction advisory lock.
        Returns (meeting, is_created).
        """
        now = datetime.now(timezone.utc)
        normalized_platform = (platform or "GOOGLE_MEET").upper().strip()
        if meeting_url and ("zoom.us" in meeting_url.lower() or "zoomgov.com" in meeting_url.lower()):
            normalized_platform = "ZOOM_WEB"

        sanitized_url = self.sanitize_meeting_url(meeting_url, normalized_platform)
        resolved_meeting_code = meeting_code or self.extract_meeting_code(sanitized_url, meeting_space_name, normalized_platform)
        meeting_code = resolved_meeting_code
        resolved_title = self.normalize_meeting_title(title, meeting_code, normalized_platform)

        if normalized_platform == "ZOOM_WEB":
            conference_ident = f"zoom_{meeting_code}" if meeting_code else (f"zoom_{meeting_space_name}" if meeting_space_name else None)
        else:
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
                default_name = "Zoom" if existing.platform == "ZOOM_WEB" else "Meet"
                generic_titles = (
                    "meet", "google meet", "google meet session", "google meet (undefined)",
                    "zoom", "zoom meeting", "zoom session", "zoom web client", "zoom (undefined)",
                )
                if (existing_lower in generic_titles or not existing.title) and resolved_title != default_name:
                    update_vals["title"] = resolved_title
                elif title and title != existing.title and title.strip().lower() not in generic_titles:
                    update_vals["title"] = resolved_title
                if sanitized_url:
                    update_vals["meeting_url"] = sanitized_url
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

        # Tier 1 & 2: Search active in_progress meeting matching conference_ident / code within platform
        from app.models.live_session import LiveSession

        lookup_terms = [t for t in [conference_ident, meeting_code, conference_record_name, meeting_space_name] if t]
        if normalized_platform == "ZOOM_WEB" and meeting_code and f"zoom_{meeting_code}" not in lookup_terms:
            lookup_terms.append(f"zoom_{meeting_code}")

        active_meeting = None
        is_stale_zoom = False
        closed_stale_room = False
        if lookup_terms:
            stmt = (
                select(Meeting)
                .where(Meeting.platform == normalized_platform)
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

            # Platform-specific active session check:
            # For ZOOM_WEB: A meeting room is only considered actively continuing if it has
            # an active capture session with a heartbeat within the last 60 seconds, or was
            # created within the last 60 seconds (new room onboarding window).
            # Stale orphan meetings (> 60s inactive) must be closed to avoid reusing old meetings
            # across distinct Zoom sessions sharing the same PMI / room URL.
            if normalized_platform == "ZOOM_WEB":
                created_tz = active_meeting.created_at.replace(tzinfo=timezone.utc) if active_meeting.created_at.tzinfo is None else active_meeting.created_at
                created_age = (now - created_tz).total_seconds()

                if max_session_hb is not None:
                    hb_tz = max_session_hb.replace(tzinfo=timezone.utc) if max_session_hb.tzinfo is None else max_session_hb
                    if (now - hb_tz).total_seconds() > 60:
                        is_stale_zoom = True
                elif created_age > 60:
                    # No active sessions recorded and created over 60 seconds ago
                    is_stale_zoom = True

            is_within_window = not is_stale_zoom and ((now - latest_activity).total_seconds() <= 1800)

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
                default_name = "Zoom" if active_meeting.platform == "ZOOM_WEB" else "Meet"
                generic_titles = (
                    "meet", "google meet", "google meet session", "google meet (undefined)",
                    "zoom", "zoom meeting", "zoom session", "zoom web client", "zoom (undefined)",
                )
                if (active_lower in generic_titles or not active_meeting.title) and resolved_title != default_name:
                    update_vals["title"] = resolved_title
                if conference_record_name and not active_meeting.conference_record_name:
                    update_vals["conference_record_name"] = conference_record_name
                if update_vals:
                    self.meeting_repo.update(active_meeting.id, update_vals)

                self.session.commit()
                return active_meeting, False
            else:
                # Expired Continuity Window (> 30 mins inactive) or Stale Zoom orphan (> 120s inactive):
                # Close stale in_progress room to 'completed' BEFORE creating new room
                # to satisfy Partial Unique Index uq_active_conference_identity
                active_meeting.status = "completed"
                active_meeting.end_time = latest_activity
                active_meeting.updated_at = now
                self.session.flush()
                active_meeting = None
                closed_stale_room = True

        # Tier 2.5: Check recently completed meeting within Grace Period (180s)
        # Allows tab reload or brief disconnection to resume same room without fragmenting.
        # Guard: Stale rooms closed above must NOT be resurrected here.
        if active_meeting is None and lookup_terms and not closed_stale_room:
            recent_completed_stmt = (
                select(Meeting)
                .where(Meeting.platform == normalized_platform)
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
                    # Refinement: Require active heartbeat or explicit end_time to determine grace period.
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
            "meeting_url": sanitized_url,
            "title": resolved_title,
            "start_time": start_time or now,
            "end_time": end_time,
            "status": "in_progress",
            "conference_identity": conference_ident,
            "last_heartbeat_at": now,
            "platform": normalized_platform,
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
                existing_retry = self.meeting_repo.find_active_by_meeting_code(
                    meeting_code=meeting_code,
                    platform=normalized_platform,
                )
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
