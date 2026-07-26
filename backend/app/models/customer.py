"""Customer — the party being debited."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import clock
from app.core.ids import new_id
from app.models.base import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("cus")
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Bank details for the update-details flow. AU direct debit is BSB +
    # account number; only the last four digits are retained, since nothing in
    # mock mode needs the full number and storing it is a liability we have no
    # reason to take on for a hackathon.
    bank_account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bank_bsb: Mapped[str | None] = mapped_column(String(7), nullable=True)
    bank_account_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    # Set when the customer submits new details, so the UI can show that the
    # update landed and the engine can tell a fresh account from a stale one.
    payment_method_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Pinch's own ids, populated only in PINCH_MODE=live. A payer is created
    # (or looked up) the first time we call update_payment_method for this
    # customer; a retry needs pinch_payer_id to know who to re-present against.
    # NULL in mock mode — the mock never opens a socket to Pinch.
    pinch_payer_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pinch_source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Monday=0 .. Sunday=6, matching strategies.yaml default_payday_weekdays.
    # NULL means "no observed history" and the engine falls back to defaults.
    observed_payday_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=clock.now
    )

    payments: Mapped[list["Payment"]] = relationship(  # noqa: F821
        back_populates="customer", cascade="all, delete-orphan"
    )
