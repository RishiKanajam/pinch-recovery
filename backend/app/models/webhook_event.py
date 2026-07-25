"""WebhookEvent — the idempotency ledger for inbound Pinch webhooks.

`event_id` carries a UNIQUE constraint, and that constraint is the idempotency
mechanism itself, not a sanity check on one. Ingest inserts here first and lets
the database reject the duplicate; checking "does it exist?" in Python first
would leave a race between the check and the insert.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core import clock
from app.core.ids import new_id
from app.models.base import Base


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("wev")
    )

    # Pinch's event id. The idempotency key — see CLAUDE.md rule 3.
    event_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )

    # The full envelope as delivered, kept verbatim so a mis-parse can be
    # diagnosed (or replayed) without asking Pinch to resend.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=clock.now
    )
