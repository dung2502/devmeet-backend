import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, values: Mapping[str, Any]) -> User:
        user = User(**values)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.session.get(User, user_id)

    def update(self, user_id: uuid.UUID, values: Mapping[str, Any]) -> User | None:
        user = self.get_by_id(user_id)
        if user is None:
            return None

        for key, value in values.items():
            setattr(user, key, value)

        self.session.commit()
        self.session.refresh(user)
        return user

    def upsert(self, values: Mapping[str, Any]) -> User:
        user = self.session.scalar(
            select(User).where(User.google_user_id == values["google_user_id"])
        )

        if user is None:
            return self.create(values)

        for key, value in values.items():
            setattr(user, key, value)

        self.session.commit()
        self.session.refresh(user)
        return user

    def list_with_pagination(self, page: int = 1, page_size: int = 50) -> list[User]:
        if page < 1:
            raise ValueError("page must be greater than or equal to 1")
        if page_size < 1:
            raise ValueError("page_size must be greater than or equal to 1")

        offset = (page - 1) * page_size
        return list(
            self.session.scalars(
                select(User).order_by(User.created_at, User.id).offset(offset).limit(page_size)
            )
        )

