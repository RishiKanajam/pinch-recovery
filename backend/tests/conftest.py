"""Shared test fixtures.

Tests run against a real Postgres — the same engine the app uses — in a
separate `pinch_recovery_test` database. Idempotency here is enforced by a
unique index, and a fake in-memory store would test our Python instead of the
constraint that actually does the work.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.models import Base

TEST_DB_NAME = "pinch_recovery_test"

# Derived from the metadata, not hand-listed: a hardcoded list silently stops
# truncating any table added later, and leftover rows then leak between tests
# as phantom data that is very hard to read as a fixture problem.
TABLES = tuple(t.name for t in Base.metadata.sorted_tables)


def _swap_database(url: str, name: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + name


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Create the test database once per session and build the schema."""
    admin_url = _swap_database(settings.DATABASE_URL, "postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"),
            {"n": TEST_DB_NAME},
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    admin.dispose()

    test_engine = create_engine(_swap_database(settings.DATABASE_URL, TEST_DB_NAME))
    Base.metadata.create_all(test_engine)
    yield test_engine
    test_engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """A clean database per test, plus a session for assertions."""
    with engine.begin() as conn:
        conn.execute(
            text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        )

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(engine: Engine, db_session: Session) -> Iterator[TestClient]:
    """TestClient wired to the test database.

    Each request gets its own session, exactly as in production — so a test
    exercises the same commit boundaries the real endpoint has.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
