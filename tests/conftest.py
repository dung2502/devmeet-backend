import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.models
from app.config import get_settings
from app.database import Base


@pytest.fixture(scope="session")
def test_schema_name() -> str:
    return f"devmeet_test_{uuid.uuid4().hex}"


@pytest.fixture(scope="session")
def test_engine(test_schema_name: str) -> Generator[Engine, None, None]:
    settings = get_settings()
    admin_engine = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{test_schema_name}"'))

    engine = create_engine(
        settings.database_url,
        connect_args={"options": f"-csearch_path={test_schema_name}"},
        pool_pre_ping=True,
    )

    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{test_schema_name}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture()
def db_session(test_engine: Engine) -> Generator[Session, None, None]:
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    session_factory = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = session_factory()

    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)


@pytest.fixture()
def test_user(db_session: Session):
    from app.models.user import User
    user = User(
        google_user_id=f"test-google-{uuid.uuid4().hex[:8]}",
        email=f"tester-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Tester DevMeet",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def mock_jwt_token(db_session: Session, test_user):
    from app.models.auth_session import AuthSession
    from app.services.token_service import TokenService
    session_record = AuthSession(
        user_id=test_user.id,
        refresh_token_hash=uuid.uuid4().hex,
        client_type="WEB",
    )
    db_session.add(session_record)
    db_session.commit()
    db_session.refresh(session_record)

    token, _ = TokenService.create_access_token(
        user=test_user,
        session_id=session_record.id,
        client_type="WEB",
    )
    return token


@pytest.fixture()
def auth_headers(mock_jwt_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mock_jwt_token}"}


