"""Declarative base. Alembic reads Base.metadata to autogenerate migrations."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
