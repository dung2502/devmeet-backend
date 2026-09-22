import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transcript


class TranscriptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, values: Mapping[str, Any]) -> Transcript:
        transcript = Transcript(**values)
        self.session.add(transcript)
        self.session.commit()
        self.session.refresh(transcript)
        return transcript

    def get_by_id(self, transcript_id: uuid.UUID) -> Transcript | None:
        return self.session.get(Transcript, transcript_id)

    def update(
        self,
        transcript_id: uuid.UUID,
        values: Mapping[str, Any],
    ) -> Transcript | None:
        transcript = self.get_by_id(transcript_id)
        if transcript is None:
            return None

        for key, value in values.items():
            setattr(transcript, key, value)

        self.session.commit()
        self.session.refresh(transcript)
        return transcript

    def upsert(self, values: Mapping[str, Any]) -> Transcript:
        transcript = self.session.scalar(
            select(Transcript).where(
                Transcript.google_transcript_name == values["google_transcript_name"]
            )
        )

        if transcript is None:
            return self.create(values)

        for key, value in values.items():
            setattr(transcript, key, value)

        self.session.commit()
        self.session.refresh(transcript)
        return transcript

    def list_with_pagination(
        self,
        page: int = 1,
        page_size: int = 50,
    ) -> list[Transcript]:
        if page < 1:
            raise ValueError("page must be greater than or equal to 1")
        if page_size < 1:
            raise ValueError("page_size must be greater than or equal to 1")

        offset = (page - 1) * page_size
        return list(
            self.session.scalars(
                select(Transcript)
                .order_by(Transcript.created_at, Transcript.id)
                .offset(offset)
                .limit(page_size)
            )
        )

