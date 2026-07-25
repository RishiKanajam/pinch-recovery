"""Attempt — one execution of a strategy against a payment."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import new_id
from app.models.base import Base
from app.models.enums import AttemptStatus


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("att")
    )
    payment_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # ActionType / Channel / AttemptStatus values from CONTRACT.md, as strings.
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    # NULL for actions with no outbound message, e.g. a retry.
    channel: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AttemptStatus.SCHEDULED.value
    )

    # Indexed because the due-action poller queries "scheduled_for <= now()"
    # on every tick, and a fast-forward fires a burst of those at once.
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    payment: Mapped["Payment"] = relationship(back_populates="attempts")  # noqa: F821
