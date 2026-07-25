"""Request schemas for the ingestion API.

The inbound envelope is specified in docs/CONTRACT.md under
"Ingestion-internal". The mock simulator emits this exact shape, so both modes
ingest identically.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PinchEventData(BaseModel):
    # Unknown fields are kept rather than rejected: Pinch adding a field must
    # not start returning 422 to their retrying webhook sender.
    model_config = ConfigDict(extra="allow")

    payment_id: str
    customer_id: str
    amount_cents: int
    currency: str = "AUD"
    # TODO verify against live webhook — the field names inside `data` are the
    # most likely thing to differ from Pinch's real shape. Pinch delivers
    # dishonours on `bank-results` events with hyphenated codes such as
    # "insufficient-funds". See docs/pinch-codes-proposal.md.
    dishonour_code: str | None = None


class PinchWebhookEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str = Field(min_length=1)
    event_type: str
    created_at: datetime | None = None
    data: PinchEventData
