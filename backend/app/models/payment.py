"""Payment — a single attempted debit. Field names match docs/CONTRACT.md."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import clock
from app.core.ids import new_id
from app.models.base import Base
from app.models.enums import PaymentStatus


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("pay")
    )
    customer_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # Integer cents. BigInteger, never Numeric or Float — see CLAUDE.md rule 1.
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="AUD")

    # Stored as a plain string, not a DB enum. Values from CONTRACT.md.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PaymentStatus.PENDING.value, index=True
    )

    # Pinch's own dishonour code, preserved verbatim. failure_class is ours,
    # derived from it by the classifier — the decoupling CONTRACT.md calls for.
    raw_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Pinch's own payment id (`pmt_...`) for the most recent presentation of
    # this debit — the original dishonour, or the latest retry. NULL in mock
    # mode. A live retry re-presents as a *new* Pinch payment, so this is
    # overwritten each time; the settlement webhook is matched back to our
    # local row by this id, not by ours, because Pinch has never heard of ours.
    pinch_payment_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )

    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The sentence the judge reads. NULL until the classifier has run.
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=clock.now
    )

    customer: Mapped["Customer"] = relationship(back_populates="payments")  # noqa: F821
    attempts: Mapped[list["Attempt"]] = relationship(  # noqa: F821
        back_populates="payment",
        cascade="all, delete-orphan",
        order_by="Attempt.attempt_number",
    )
