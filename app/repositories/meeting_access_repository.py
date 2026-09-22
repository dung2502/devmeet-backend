import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.meeting_access import MeetingAccess, MeetingRole


class MeetingAccessRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_access(self, meeting_id: uuid.UUID, user_id: uuid.UUID) -> MeetingAccess | None:
        return self.session.scalar(
            select(MeetingAccess).where(
                MeetingAccess.meeting_id == meeting_id,
                MeetingAccess.user_id == user_id,
            )
        )

    def add_or_update_access(
        self,
        meeting_id: uuid.UUID,
        user_id: uuid.UUID,
        role: str = MeetingRole.PARTICIPANT.value,
        last_seen_at: datetime | None = None,
    ) -> MeetingAccess:
        access = self.get_access(meeting_id, user_id)
        if access is not None:
            if role == MeetingRole.OWNER.value:
                access.role = MeetingRole.OWNER.value
            if last_seen_at is not None:
                access.last_seen_at = last_seen_at
            self.session.flush()
            return access

        now = datetime.now(timezone.utc)
        access = MeetingAccess(
            meeting_id=meeting_id,
            user_id=user_id,
            role=role,
            first_joined_at=last_seen_at or now,
            last_seen_at=last_seen_at or now,
        )
        self.session.add(access)
        self.session.flush()
        return access

    def list_by_meeting_id(self, meeting_id: uuid.UUID) -> list[MeetingAccess]:
        return list(
            self.session.scalars(
                select(MeetingAccess)
                .where(MeetingAccess.meeting_id == meeting_id)
                .order_by(MeetingAccess.first_joined_at)
            )
        )

    def list_by_user_id(self, user_id: uuid.UUID) -> list[MeetingAccess]:
        return list(
            self.session.scalars(
                select(MeetingAccess)
                .where(MeetingAccess.user_id == user_id)
                .order_by(MeetingAccess.last_seen_at.desc())
            )
        )

    def get_user_role(self, meeting_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
        access = self.get_access(meeting_id, user_id)
        return access.role if access else None

    def remove_access(self, meeting_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        access = self.get_access(meeting_id, user_id)
        if access is not None:
            self.session.delete(access)
            self.session.commit()
            return True
        return False
