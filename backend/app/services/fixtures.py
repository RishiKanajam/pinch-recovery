"""Stub dataset standing in for Person A's Postgres tables.

Person B builds against this so neither half blocks the other (see the README
sequencing: dashboard reads stub data matching the contract until first
integration). It is deliberately plain data — no ORM, no DB — so that swapping
in Person A's repository later is a change of one module, not a rewrite.

The rows below are shaped to docs/CONTRACT.md. Real AU service-business names
and plausible amounts, because a dashboard full of "Test Customer 1" and
$10.00 reads as a toy in a demo.

Deterministic: the RNG is seeded, so /sim/reset always produces the same
dashboard and the demo script stays true across rehearsals.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# Raw dishonour codes per class, mirroring strategies.yaml. Kept as literals
# rather than read from the YAML so that a seed row exercising a code the
# classifier does not know is a visible, intentional test of the `unknown`
# fallback rather than an impossible state.
CODES_BY_CLASS: dict[str, list[str]] = {
    "insufficient_funds": ["AM04", "0902", "insufficient_funds", "refer_to_issuer"],
    "invalid_account": ["AC01", "AC04", "0903", "account_closed"],
    "authority_cancelled": ["MD01", "0905", "mandate_cancelled"],
    "payment_stopped": ["MS02", "0904", "stop_payment"],
    "technical": ["AG01", "0909", "processing_error", "timeout"],
    "expired_card": ["54", "expired_card"],
    "do_not_honour": ["05", "do_not_honour", "generic_decline"],
    # Not in strategies.yaml on purpose — proves the unknown path renders.
    "unknown": ["ZZ99", "XX01"],
}

# Roughly the shape of a real dishonour mix: insufficient funds dominates,
# hard failures are a meaningful minority, unknown is rare but present.
CLASS_WEIGHTS: dict[str, int] = {
    "insufficient_funds": 20,
    "invalid_account": 8,
    "authority_cancelled": 6,
    "payment_stopped": 3,
    "technical": 5,
    "expired_card": 4,
    "do_not_honour": 3,
    "unknown": 2,
}

CUSTOMERS: list[tuple[str, str]] = [
    ("Marina Auto Detailing", "accounts@marinadetailing.com.au"),
    ("Brunswick Pilates Studio", "hello@brunswickpilates.com.au"),
    ("Kerbside Coffee Roasters", "ap@kerbsideroasters.com.au"),
    ("Northcote Dog Grooming", "bookings@northcotedog.com.au"),
    ("Glenferrie Physio Group", "admin@glenferriephysio.com.au"),
    ("Salted Lime Catering", "accounts@saltedlime.com.au"),
    ("Fitzroy Bike Workshop", "shop@fitzroybikes.com.au"),
    ("Coastal Lawn & Garden", "jobs@coastallawn.com.au"),
    ("Redfern Yoga Collective", "studio@redfernyoga.com.au"),
    ("Two Birds Bookkeeping", "hello@twobirdsbooks.com.au"),
    ("Sandringham Swim School", "office@sandyswim.com.au"),
    ("Preston Panel & Paint", "accounts@prestonpanel.com.au"),
    ("Herbert St Dental", "reception@herbertstdental.com.au"),
    ("Willow Creek Landscaping", "quotes@willowcreekland.com.au"),
    ("Ascot Vale Veterinary", "vet@ascotvalevet.com.au"),
    ("Buller Snow Hire", "hire@bullersnow.com.au"),
    ("Cardigan St Chiropractic", "front@cardiganchiro.com.au"),
    ("Pace & Co Accounting", "billing@paceandco.com.au"),
    ("Thornbury Tile Supply", "sales@thornburytile.com.au"),
    ("Barkly Square Cleaners", "ops@barklyclean.com.au"),
]

# Monthly subscription-ish amounts in integer cents. Service businesses on
# direct debit, so mostly recurring plan prices rather than random figures.
AMOUNTS_CENTS: list[int] = [
    4900, 6900, 8900, 9900, 12900, 14900, 17900, 19900,
    24900, 29900, 34900, 39900, 49900, 59900, 79900, 99900,
    124900, 149900, 199900, 249900,
]

BSBS: list[str] = ["063-000", "083-004", "013-006", "923-100", "633-000", "704-191"]


@dataclass
class RawCustomer:
    """A customer plus the payment-method details the update page edits."""

    id: str
    name: str
    email: str
    account_name: str
    bsb: str
    account_number: str
    # Weekday (Mon=0) this customer is observed to be paid on. None means the
    # engine falls back to global_rules.default_payday_weekdays. Only some
    # customers have observed history, which is realistic and exercises both
    # branches of the payday-alignment logic.
    payday_weekday: int | None = None


@dataclass
class RawPayment:
    """A failed debit before the engine has looked at it.

    No failure_class and no reasoning — those are the engine's output, not seed
    data. Seeding them here would make the demo a lie.
    """

    id: str
    customer_id: str
    amount_cents: int
    raw_code: str
    # Hours before "now" that this payment failed. Drives the write-off horizon
    # and the message-frequency cap having something to actually bite on.
    failed_hours_ago: int
    # Seeded terminal outcomes, so the dashboard has recovered and written-off
    # money on first load rather than four zeroes. None = still in flight.
    seeded_outcome: str | None = None
    attempts_before_outcome: int = 0
    tags: list[str] = field(default_factory=list)


def _slug(n: int, prefix: str) -> str:
    """ULID-ish ids. Not real ULIDs — stable and readable beats correct here."""
    return f"{prefix}_01HX{n:04d}"


def build_customers(rng: random.Random) -> list[RawCustomer]:
    customers: list[RawCustomer] = []
    for i, (name, email) in enumerate(CUSTOMERS):
        customers.append(
            RawCustomer(
                id=_slug(i, "cus"),
                name=name,
                email=email,
                account_name=name,
                bsb=rng.choice(BSBS),
                account_number=str(rng.randint(100000, 99999999)),
                # ~40% have observed payday history.
                payday_weekday=rng.choice([3, 4, 2, 0]) if rng.random() < 0.4 else None,
            )
        )
    return customers


def build_payments(rng: random.Random, customers: list[RawCustomer]) -> list[RawPayment]:
    """~50 failed payments across all eight classes.

    Outcomes are assigned per class in a way that matches the product thesis:
    insufficient_funds recovers often, hard failures mostly do not recover
    without a details update, payment_stopped never auto-recovers.
    """
    payments: list[RawPayment] = []
    n = 0
    for failure_class, weight in CLASS_WEIGHTS.items():
        codes = CODES_BY_CLASS[failure_class]
        for _ in range(weight):
            customer = rng.choice(customers)
            outcome = _pick_outcome(rng, failure_class)
            payments.append(
                RawPayment(
                    id=_slug(n, "pay"),
                    customer_id=customer.id,
                    amount_cents=rng.choice(AMOUNTS_CENTS),
                    raw_code=rng.choice(codes),
                    failed_hours_ago=rng.choice([2, 6, 18, 30, 54, 96, 160, 240, 400, 520]),
                    seeded_outcome=outcome,
                    attempts_before_outcome=rng.randint(1, 3) if outcome else 0,
                    tags=[failure_class],
                )
            )
            n += 1

    rng.shuffle(payments)
    return payments


def _pick_outcome(rng: random.Random, failure_class: str) -> str | None:
    """Terminal outcome for a seeded payment, or None if still in flight.

    The probabilities encode the thesis the demo argues: a timing failure is
    recoverable, a data failure needs the customer to act, and a dispute is not
    an automation problem at all.
    """
    roll = rng.random()
    if failure_class == "insufficient_funds":
        if roll < 0.62:
            return "recovered"
        if roll < 0.72:
            return "written_off"
        return None
    if failure_class == "technical":
        if roll < 0.80:
            return "recovered"
        return None
    if failure_class == "expired_card":
        if roll < 0.45:
            return "recovered"
        return None
    if failure_class == "do_not_honour":
        if roll < 0.35:
            return "recovered"
        if roll < 0.50:
            return "written_off"
        return None
    if failure_class == "invalid_account":
        if roll < 0.30:
            return "recovered"
        if roll < 0.55:
            return "written_off"
        return None
    if failure_class == "authority_cancelled":
        if roll < 0.12:
            return "recovered"
        if roll < 0.60:
            return "written_off"
        return None
    if failure_class == "payment_stopped":
        # Never auto-recovers. A human resolves it or it is written off.
        return "written_off" if roll < 0.5 else None
    return None  # unknown: always left in flight, awaiting a human
