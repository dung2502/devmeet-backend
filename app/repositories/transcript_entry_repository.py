import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TranscriptEntry


class TranscriptEntryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, values: Mapping[str, Any]) -> TranscriptEntry:
        entry = TranscriptEntry(**values)
        self.session.add(entry)
        self.session.commit()
        self.session.refresh(entry)
        return entry

    def get_by_id(self, entry_id: uuid.UUID) -> TranscriptEntry | None:
        return self.session.get(TranscriptEntry, entry_id)

    def update(
        self,
        entry_id: uuid.UUID,
        values: Mapping[str, Any],
    ) -> TranscriptEntry | None:
        entry = self.get_by_id(entry_id)
        if entry is None:
            return None

        for key, value in values.items():
            setattr(entry, key, value)

        self.session.commit()
        self.session.refresh(entry)
        return entry

    def upsert(self, values: Mapping[str, Any]) -> TranscriptEntry:
        entry = self.session.scalar(
            select(TranscriptEntry).where(
                TranscriptEntry.google_entry_name == values["google_entry_name"]
            )
        )

        if entry is None:
            return self.create(values)

        for key, value in values.items():
            setattr(entry, key, value)

        self.session.commit()
        self.session.refresh(entry)
        return entry

    def list_with_pagination(
        self,
        page: int = 1,
        page_size: int = 50,
    ) -> list[TranscriptEntry]:
        if page < 1:
            raise ValueError("page must be greater than or equal to 1")
        if page_size < 1:
            raise ValueError("page_size must be greater than or equal to 1")

        offset = (page - 1) * page_size
        return list(
            self.session.scalars(
                select(TranscriptEntry)
                .order_by(TranscriptEntry.created_at, TranscriptEntry.id)
                .offset(offset)
                .limit(page_size)
            )
        )

