"""Request schemas for the ingestion API.

The inbound envelope is specified in docs/CONTRACT.md under
"Ingestion-internal". The mock simulator emits this exact shape, so both modes
ingest identically.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer


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


# --------------------------------------------------------------------------
# Response shapes — docs/CONTRACT.md "Core objects"
# --------------------------------------------------------------------------


def _z(value: datetime | None) -> str | None:
    """Serialise as 2026-07-25T04:12:00Z, the format in the contract.

    Pydantic's default emits +00:00. Same instant, different string, and the
    contract shows Z — not worth Person B discovering the difference in a
    string comparison.
    """
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    payment_id: str
    action: str
    channel: str | None
    status: str
    scheduled_for: datetime | None
    executed_at: datetime | None
    attempt_number: int
    note: str | None

    @field_serializer("scheduled_for", "executed_at")
    def _ser(self, value: datetime | None) -> str | None:
        return _z(value)


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str
    # Denormalised from the customer so the dashboard need not join.
    customer_name: str
    amount_cents: int
    currency: str
    status: str
    raw_code: str | None
    # NULL until Person B's classifier runs. Present in the shape regardless,
    # so their code binds against a stable field either way.
    failure_class: str | None
    failed_at: datetime | None
    recovered_at: datetime | None
    attempts: list[AttemptOut] = []
    reasoning: str | None

    @field_serializer("failed_at", "recovered_at")
    def _ser(self, value: datetime | None) -> str | None:
        return _z(value)


class PaymentList(BaseModel):
    data: list[PaymentOut]
    # Opaque; pass back as ?cursor= for the next page. Null on the last page.
    next_cursor: str | None = None
