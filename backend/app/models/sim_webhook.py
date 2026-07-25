"""A webhook the simulator will deliver to us later.

Persisted rather than held in memory: `uvicorn --reload` restarts on any file
save, and losing the queue mid-demo would look exactly like the simulator not
working. It also means /sim/reset clears deliveries with everything else.

`event_id` is deliberately NOT unique here. Requesting two deliveries writes
two rows carrying the same event_id — that is the point, and the uniqueness
that matters lives on webhook_events, downstream.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.models.base import Base


class SimulatedWebhook(Base):
    __tablename__ = "sim_pending_webhooks"

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("simw")
    )

    event_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Measured on the simulated clock, so a fast-forward makes it due.
    deliver_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 1-based, so a redelivery is distinguishable in the response.
    delivery_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
