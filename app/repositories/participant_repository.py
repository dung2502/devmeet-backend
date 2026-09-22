import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Participant


class ParticipantRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, values: Mapping[str, Any]) -> Participant:
        participant = Participant(**values)
        self.session.add(participant)
        self.session.commit()
        self.session.refresh(participant)
        return participant

    def get_by_id(self, participant_id: uuid.UUID) -> Participant | None:
        return self.session.get(Participant, participant_id)

    def update(
        self,
        participant_id: uuid.UUID,
        values: Mapping[str, Any],
    ) -> Participant | None:
        participant = self.get_by_id(participant_id)
        if participant is None:
            return None

        for key, value in values.items():
            setattr(participant, key, value)

        self.session.commit()
        self.session.refresh(participant)
        return participant

    def upsert(self, values: Mapping[str, Any]) -> Participant:
        participant = self.session.scalar(
            select(Participant).where(
                Participant.meeting_id == values["meeting_id"],
                Participant.google_participant_name == values["google_participant_name"],
            )
        )

        if participant is None:
            return self.create(values)

        for key, value in values.items():
            setattr(participant, key, value)

        self.session.commit()
        self.session.refresh(participant)
        return participant

    def list_with_pagination(
        self,
        page: int = 1,
        page_size: int = 50,
    ) -> list[Participant]:
        if page < 1:
            raise ValueError("page must be greater than or equal to 1")
        if page_size < 1:
            raise ValueError("page_size must be greater than or equal to 1")

        offset = (page - 1) * page_size
        return list(
            self.session.scalars(
                select(Participant)
                .order_by(Participant.created_at, Participant.id)
                .offset(offset)
                .limit(page_size)
            )
        )

