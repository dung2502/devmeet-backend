import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Meeting


class MeetingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, values: Mapping[str, Any]) -> Meeting:
        meeting = Meeting(**values)
        self.session.add(meeting)
        self.session.commit()
        self.session.refresh(meeting)
        return meeting

    def get_by_id(self, meeting_id: uuid.UUID) -> Meeting | None:
        return self.session.get(Meeting, meeting_id)

    def update(self, meeting_id: uuid.UUID, values: Mapping[str, Any]) -> Meeting | None:
        meeting = self.get_by_id(meeting_id)
        if meeting is None:
            return None

        for key, value in values.items():
            setattr(meeting, key, value)

        self.session.commit()
        self.session.refresh(meeting)
        return meeting

    def create_extension_meeting(self, values: Mapping[str, Any]) -> Meeting:
        return self.create(values)

    def upsert(self, values: Mapping[str, Any]) -> Meeting:
        conf_name = values.get("conference_record_name")
        if not conf_name:
            return self.create(values)

        meeting = self.session.scalar(
            select(Meeting).where(
                Meeting.conference_record_name == conf_name
            )
        )

        if meeting is None:
            return self.create(values)

        for key, value in values.items():
            setattr(meeting, key, value)

        self.session.commit()
        self.session.refresh(meeting)
        return meeting

    def list_with_pagination(self, page: int = 1, page_size: int = 50) -> list[Meeting]:
        if page < 1:
            raise ValueError("page must be greater than or equal to 1")
        if page_size < 1:
            raise ValueError("page_size must be greater than or equal to 1")

        offset = (page - 1) * page_size
        return list(
            self.session.scalars(
                select(Meeting)
                .order_by(Meeting.created_at, Meeting.id)
                .offset(offset)
                .limit(page_size)
            )
        )

    def get_by_id_and_user(self, meeting_id: uuid.UUID, user_id: uuid.UUID) -> Meeting | None:
        """
        Return the Meeting only if it belongs to the given user.
        Returns None if the meeting does not exist OR is owned by a different user.
        Fail-closed ownership check for Phase 5D /ended.
        """
        return self.session.scalar(
            select(Meeting).where(
                Meeting.id == meeting_id,
                Meeting.user_id == user_id,
            )
        )

    def get_by_conference_identity(self, conference_identity: str) -> Meeting | None:
        if not conference_identity:
            return None
        return self.session.scalar(
            select(Meeting).where(
                (Meeting.conference_identity == conference_identity)
                | (Meeting.conference_record_name == conference_identity)
            )
        )

    def find_active_by_meeting_code(
        self,
        meeting_code: str,
        window_minutes: int = 60,
    ) -> Meeting | None:
        """
        Finds an active shared meeting matching a meeting_code / space_name within the active window.
        Checks status == 'in_progress' and active heartbeats or non-expired grace period.
        """
        if not meeting_code:
            return None
        clean_code = meeting_code.strip()
        stmt = (
            select(Meeting)
            .where(
                (Meeting.conference_identity == clean_code)
                | (Meeting.meeting_space_name == clean_code)
                | (Meeting.meeting_url.ilike(f"%{clean_code}%"))
            )
            .where(Meeting.status == "in_progress")
            .order_by(Meeting.created_at.desc())
        )
        return self.session.scalar(stmt)

    def list_by_user(
        self,
        user_id: uuid.UUID,
        page: int = 1,
        page_size: int = 50,
        sort: str = "start_time_desc",
        search: str | None = None,
    ) -> tuple[list[Meeting], int]:
        """
        List meetings accessible to user_id (owned or granted via meeting_access).
        Supports pagination, sorting, and keyword search.
        """
        from app.models.meeting_access import MeetingAccess

        if page < 1:
            page = 1
        if page_size < 1 or page_size > 500:
            page_size = 50

        # Subquery for meetings accessible via meeting_access
        access_subquery = (
            select(MeetingAccess.meeting_id)
            .where(MeetingAccess.user_id == user_id)
        )

        stmt = select(Meeting).where(
            (Meeting.user_id == user_id) | (Meeting.id.in_(access_subquery))
        )

        if search and search.strip():
            term = f"%{search.strip()}%"
            stmt = stmt.where(
                (Meeting.title.ilike(term)) | (Meeting.meeting_url.ilike(term))
            )

        total = self.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0

        if sort == "created_at_desc":
            stmt = stmt.order_by(Meeting.created_at.desc(), Meeting.id)
        elif sort == "created_at_asc":
            stmt = stmt.order_by(Meeting.created_at.asc(), Meeting.id)
        elif sort == "start_time_asc":
            stmt = stmt.order_by(Meeting.start_time.asc().nulls_last(), Meeting.created_at.asc(), Meeting.id)
        else:  # default "start_time_desc"
            stmt = stmt.order_by(Meeting.start_time.desc().nulls_last(), Meeting.created_at.desc(), Meeting.id)

        offset = (page - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size)
        items = list(self.session.scalars(stmt))
        return items, total

    def delete(self, meeting_id: uuid.UUID) -> bool:
        """
        Delete a meeting by ID, cascading deletes to all child entities
        (participants, transcripts, entries, live sessions, access records, dom segments).
        Returns True if deleted, False if meeting was not found.
        """
        meeting = self.get_by_id(meeting_id)
        if meeting is None:
            return False
        self.session.delete(meeting)
        self.session.commit()
        return True


