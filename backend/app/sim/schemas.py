"""Request schemas for the simulator. Shapes from docs/CONTRACT.md."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ScenarioRequest(BaseModel):
    # Either an existing customer, or a name to create one under. Both may be
    # omitted and a placeholder is generated.
    customer_id: str | None = None
    customer_name: str | None = None

    amount_cents: int = Field(gt=0)
    outcome: Literal["dishonour", "success"] = "dishonour"

    # Pinch's dishonour code, verbatim — hyphenated lowercase, e.g.
    # "insufficient-funds". See docs/pinch-codes-proposal.md.
    raw_code: str | None = None

    # Settlement window. Three days is realistic for a direct debit dishonour;
    # one fast-forward collapses it.
    delay_seconds: int = Field(default=0, ge=0)

    # 2 delivers the identical event twice, to exercise idempotency.
    webhook_deliveries: int = Field(default=1, ge=1, le=10)


class FastForwardRequest(BaseModel):
    seconds: float = Field(ge=0)
