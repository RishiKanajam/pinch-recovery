"""Database engine, session factory, and the FastAPI session dependency.

create_engine does not open a connection, so importing this module (and
serving /health) works with Postgres down. The first real query is what
requires the database to be up.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    # Recycles connections the database dropped underneath us — cheap
    # insurance against a stale pool after a fast-forward demo pause.
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    # Keeps attributes readable after commit, so a handler can return the
    # object it just wrote without triggering a fresh SELECT.
    expire_on_commit=False,
)


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
