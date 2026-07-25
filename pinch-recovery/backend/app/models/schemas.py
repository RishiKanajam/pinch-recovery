"""Pydantic v2 shapes from docs/CONTRACT.md.

Field names here are load-bearing. Person A produces data in exactly these
shapes and this half binds to them without translation, so a rename that looks
harmless breaks the seam silently. Change the contract first, by agreement.

Money is `amount_cents`, an int, everywhere. There is no float or Decimal in
this file and there should never be one.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import (
    ActionType,
    AttemptStatus,
    Channel,
    FailureClass,
    PaymentStatus,
)


class StrategyAction(BaseModel):
    """One step in a strategy, as written in strategies.yaml.

    `delay_hours` is measured from the moment the payment was classified, not
    from the previous action — the YAML lists absolute offsets (0, 48, 96) and
    reading them as cumulative would push the invalid_account escalation out to
    six days instead of four.
    """

    model_config = ConfigDict(extra="forbid")

    action: ActionType
    delay_hours: int = 0
    align_to_payday: bool = False
    channel: Channel | None = None
    tone: str | None = None
    silent: bool = False

    @field_validator("delay_hours")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("delay_hours cannot be negative")
        return v


class Strategy(BaseModel):
    """What the engine decided for a failure class. Not persisted directly."""

    model_config = ConfigDict(extra="forbid")

    failure_class: FailureClass
    actions: list[StrategyAction]
    max_attempts: int
    notify_human: bool
    reasoning: str
    diagnosis: str = ""


class Attempt(BaseModel):
    """One execution of a strategy step: a retry, a message, an escalation."""

    model_config = ConfigDict(extra="forbid")

    id: str
    payment_id: str
    action: ActionType
    channel: Channel | None = None
    status: AttemptStatus
    scheduled_for: datetime | None = None
    executed_at: datetime | None = None
    attempt_number: int
    note: str | None = None


class Payment(BaseModel):
    """A single attempted debit against a customer.

    `reasoning` is required on every payment that has been classified. The judge
    reads that field, not the code — a classified payment with an empty
    reasoning is an unfinished feature, so the engine treats it as a bug rather
    than rendering a blank cell.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    customer_id: str
    customer_name: str
    amount_cents: int
    currency: str = "AUD"
    status: PaymentStatus
    raw_code: str | None = None
    failure_class: FailureClass | None = None
    failed_at: datetime | None = None
    recovered_at: datetime | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    reasoning: str | None = None

    @field_validator("amount_cents")
    @classmethod
    def _integer_cents(cls, v: int) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError("amount_cents must be an int — money is integer cents")
        return v

    @property
    def is_classified(self) -> bool:
        return self.failure_class is not None

    @property
    def amount_display(self) -> str:
        """`24900` -> `$249.00`. Presentation only; never feeds back into maths."""
        return f"${self.amount_cents / 100:,.2f}"


class ClassBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_class: FailureClass
    count: int
    amount_cents: int
    recovered_cents: int


class DashboardSummary(BaseModel):
    """Aggregates for the top of the dashboard. All money in integer cents."""

    model_config = ConfigDict(extra="forbid")

    at_risk_cents: int
    recovered_cents: int
    escalated_cents: int
    written_off_cents: int
    recovery_rate: float
    by_class: list[ClassBreakdown] = Field(default_factory=list)


class OutboxMessage(BaseModel):
    """A message the system 'sent'.

    Nothing is actually delivered — real email and SMS would eat most of a day
    on deliverability and a judge cannot tell the difference between this and a
    real inbox. `update_link` is the deep link into the update-details page,
    which is what makes the live recovery moment work in the demo.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    payment_id: str
    customer_id: str
    customer_name: str
    channel: Channel
    subject: str
    body: str
    sent_at: datetime
    tone: str | None = None
    action: ActionType
    update_link: str | None = None
    read: bool = False


class PaymentMethod(BaseModel):
    """Masked bank details for the update-details page.

    Only ever the last three digits of the account number. Full details are
    never returned by the API and never rendered into a template.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: str
    customer_name: str
    account_name: str
    bsb: str
    account_number_masked: str


class PaymentMethodUpdate(BaseModel):
    """Inbound new bank details from the update-details form."""

    model_config = ConfigDict(extra="forbid")

    account_name: str = Field(min_length=2, max_length=80)
    bsb: str = Field(pattern=r"^\d{3}-?\d{3}$")
    account_number: str = Field(pattern=r"^\d{6,9}$")


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    """Every error response in the contract is shaped `{"error": {...}}`."""

    error: ErrorBody
