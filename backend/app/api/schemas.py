"""Request schemas for the ingestion API.

The inbound envelope is specified in docs/CONTRACT.md under
"Ingestion-internal". Verified against
https://docs.getpinch.com.au/docs/webhooks and
https://docs.getpinch.com.au/docs/handle-dishonoured-direct-debit on
2026-07-26. The mock simulator (app/sim/service.py) emits this exact shape,
so both modes ingest identically.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.core.clock import to_iso_z

# Pinch's default wire format is PascalCase (e.g. "Id", "EventDate"); a
# camelCase format is also offered but PascalCase is what the docs' own
# examples use, and what the mock simulator emits. `populate_by_name=True`
# on every model below also accepts the lower_snake_case field names, which
# this codebase's tests and the simulator use for readability.

# Direct debit settlement — success or dishonour — arrives on exactly one
# event type, delivered once per overnight processing run and covering every
# payment settled that run, not one event per payment. There is no separate
# `payment.succeeded` event.
EVENT_BANK_RESULTS = "bank-results"

# Statuses `data.payments[i].status` may hold on a `bank-results` event.
STATUS_DISHONOURED = "dishonoured"
# "approved" is what Pinch's own documented bank-results example uses, and
# what the mock simulator emits. "settled" is accepted too in case a real
# batch reports the later terminal state instead — accepting both here rather
# than picking one at random (a `set`'s iteration order is not stable across
# processes, PYTHONHASHSEED-randomised like every str hash) is deliberate.
STATUS_APPROVED = "approved"
STATUS_SUCCEEDED = frozenset({STATUS_APPROVED, "settled"})


class PinchDishonour(BaseModel):
    """`Dishonour` on a `bank-results` payment entry. Present only when
    `status == "dishonoured"`."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Hyphenated lowercase, e.g. "insufficient-funds" — see
    # docs/pinch-codes-proposal.md for the full verified set.
    type: str = Field(alias="Type")
    description: str | None = Field(default=None, alias="Description")


class PinchPayer(BaseModel):
    """`Payer` on a `bank-results` payment entry — who was debited."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(alias="Id")
    email: str | None = Field(default=None, alias="Email")


class PinchPaymentResult(BaseModel):
    """One entry in a `bank-results` event's `Data.Payments` array."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(alias="Id")
    status: str = Field(alias="Status")
    amount: int = Field(alias="Amount")
    dishonour: PinchDishonour | None = Field(default=None, alias="Dishonour")
    payer: PinchPayer | None = Field(default=None, alias="Payer")


class PinchEventData(BaseModel):
    # Unknown fields are kept rather than rejected: Pinch adding a field must
    # not start returning 422 to their retrying webhook sender.
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    payments: list[PinchPaymentResult] = Field(default_factory=list, alias="Payments")


class PinchWebhookEvent(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    event_id: str = Field(alias="Id", min_length=1)
    event_type: str = Field(alias="Type")
    created_at: datetime | None = Field(default=None, alias="EventDate")
    data: PinchEventData = Field(alias="Data")


# --------------------------------------------------------------------------
# Response shapes — docs/CONTRACT.md "Core objects"
# --------------------------------------------------------------------------


_z = to_iso_z


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
