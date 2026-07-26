"""OutboxMessage — a message the system "sent".

Nothing is delivered; the fake inbox in the UI reads these rows. It is a table
rather than Person B's original in-memory list because the messages have to
survive the process: the demo sends a message, fast-forwards, reloads the page,
and the judge expects the inbox to still hold what they just watched arrive.

Being in Base.metadata also means `/sim/reset` truncates it along with
everything else — reset_all derives its table list from the metadata, so an
outbox left outside it would leak seeded messages across demo runs.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.models.base import Base


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("msg")
    )
    payment_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    customer_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Denormalised so the inbox renders without joining customers. The name is
    # a display string at send time; if the customer is later renamed, what we
    # sent does not retroactively change.
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Channel / ActionType values from docs/CONTRACT.md, as strings — same
    # choice as the other tables, for the same reason (no ALTER TYPE mid-demo).
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)

    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Deep link into the update-details page. This is what makes the live
    # recovery moment work — the judge clicks it out of the fake inbox.
    update_link: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Indexed: the inbox is ordered newest-first on every page load, and a
    # fast-forward can write a burst of these at once.
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
