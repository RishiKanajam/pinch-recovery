"""The demo dataset: one realistic month for an Australian service business.

~500 customers on direct debit, ~50 of whose payments failed this month. This
is what the judge looks at, so it has to read as a real merchant's ledger
rather than fifty rows of `Test Customer 1 — $100.00`.

Two design choices worth knowing:

**raw_code values are read from strategies.yaml, not hardcoded here.** The seed
and the classifier must agree on the same strings or every payment buckets to
`unknown` and the dashboard shows one strategy instead of six. Deriving them
from the classifier's own table means they cannot drift — including while the
corrected-codes patch (docs/strategies-raw-codes.patch) is still pending.

**Deterministic.** One fixed RNG seed, so a rehearsal and the live run produce
identical counts and totals. Row ids are ULIDs and still differ.

Everything is seeded through the real webhook ingest path, so each payment has
a genuine webhook_events row behind it. Nothing here writes a payment directly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.core import clock
from app.models import Customer, Payment, SimulatedWebhook
from app.sim.service import STATUS_FOR_OUTCOME, _build_envelope, deliver_due, reset_all

# Bumping this changes the whole dataset. Don't, mid-rehearsal.
SEED = 20260725

STRATEGIES_PATH = (
    Path(__file__).resolve().parent.parent / "services" / "strategies.yaml"
)

# A merchant of this size makes 50 failures in a month look like business as
# usual rather than a catastrophe.
CUSTOMER_COUNT = 500

# Failures spread across the write-off horizon from strategies.yaml, so the
# dashboard shows a range of ages rather than fifty simultaneous failures.
SPREAD_DAYS = 20

# Distinct customers carrying the 50 failures, and how many of them failed
# more than once. The retry budget in strategies.yaml is per *customer*, so
# without repeat offenders that rule can never fire in the demo.
FAILING_CUSTOMERS = 38
REPEAT_OFFENDERS = 6
FAILURES_PER_REPEAT = 3


# --------------------------------------------------------------------------
# Customer names
# --------------------------------------------------------------------------

SUBURBS: tuple[str, ...] = (
    "Bondi", "Coogee", "Manly", "Newtown", "Redfern", "Parramatta", "Glebe",
    "Surry Hills", "Cronulla", "Balmain", "Marrickville", "Chatswood",
    "Mosman", "Darlinghurst", "Pyrmont", "Rozelle",
    "Carlton", "Fitzroy", "Brunswick", "St Kilda", "Richmond", "Toorak",
    "Yarraville", "Footscray", "Preston", "Coburg",
    "Toowong", "New Farm", "Bulimba", "Paddington", "Ashgrove",
    "Woolloongabba", "Indooroopilly", "Chermside",
    "Glenelg", "Norwood", "Unley", "Prospect",
    "Fremantle", "Subiaco", "Cottesloe", "Scarborough", "Joondalup",
    "Hobart", "Battery Point", "Sandy Bay",
    "Geelong", "Ballarat", "Bendigo", "Torquay",
)

# (trade name, email slug) — the service businesses that live on direct debit.
TRADES: tuple[tuple[str, str], ...] = (
    ("Auto Detailing", "detailing"),
    ("Physio", "physio"),
    ("Cleaning Co", "cleaning"),
    ("Fitness", "fitness"),
    ("Music Tuition", "music"),
    ("Lawn & Garden Care", "lawn"),
    ("Dog Grooming", "dogs"),
    ("Dental Studio", "dental"),
    ("Pest Control", "pest"),
    ("Mobile Mechanics", "mechanics"),
    ("Pilates Studio", "pilates"),
    ("Skin Clinic", "skin"),
    ("Plumbing & Gas", "plumbing"),
    ("Tutoring Centre", "tutoring"),
)


def _slug(text: str) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum())


# --------------------------------------------------------------------------
# Failure mix
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassPlan:
    """How many failures of one class to seed, and what they cost."""

    failure_class: str
    count: int
    # Inclusive dollar range for a recurring service invoice.
    min_dollars: int
    max_dollars: int


# Weighted to look like reality: insufficient funds dominates a real dishonour
# ledger, deliberate cancellations are rare, disputes rarer still.
#
# Amounts stay inside the $49-$499 recurring-invoice band; the mix is tuned so
# total exposure lands in the demo-friendly $15-20k range rather than by
# inflating any single invoice past what a service business would charge.
SEED_PLAN: tuple[ClassPlan, ...] = (
    ClassPlan("insufficient_funds", 20, 119, 449),
    ClassPlan("invalid_account", 14, 199, 499),
    ClassPlan("do_not_honour", 12, 219, 499),
    ClassPlan("technical", 2, 99, 349),
    ClassPlan("authority_cancelled", 1, 249, 499),
    ClassPlan("payment_stopped", 1, 299, 499),
)

# Guard rails for the headline number the judge sees.
AT_RISK_FLOOR_CENTS = 1_500_000
AT_RISK_CEILING_CENTS = 2_000_000

TOTAL_SEEDED = sum(plan.count for plan in SEED_PLAN)

# expired_card is deliberately absent.
#
# A card cannot expire on a direct debit — invalid-card and unsupported-card are
# card-scheme failures, and this is a direct debit recovery product. Seeding it
# puts a class on screen that invites "why is there a card expiry in a direct
# debit demo?", which is a credibility question, not a feature. Its payments
# were reallocated to the classes that are real for direct debit.
#
# The class stays in strategies.yaml: the engine handling it shows range, and a
# card-funded subscription would legitimately produce it. It is the demo
# dataset it does not belong in. To reinstate, add a ClassPlan above.
EXCLUDED_FROM_SEED = ("expired_card",)


@lru_cache(maxsize=1)
def strategy_raw_codes() -> dict[str, tuple[str, ...]]:
    """Read each failure class's raw_codes from strategies.yaml.

    Not duplicated here on purpose. If the seed hardcoded its own copy, the two
    would silently disagree the moment either changed, and the symptom would be
    fifty payments classified `unknown` with nothing in the logs to explain it.
    """
    data = yaml.safe_load(STRATEGIES_PATH.read_text())
    return {
        name: tuple(body.get("raw_codes") or ())
        for name, body in (data.get("classes") or {}).items()
    }


def _codes_for(failure_class: str) -> tuple[str, ...]:
    codes = strategy_raw_codes().get(failure_class)
    if not codes:
        # Loud, because the alternative is a demo that silently shows one
        # strategy instead of six.
        raise RuntimeError(
            f"strategies.yaml has no raw_codes for '{failure_class}'. The seed "
            "cannot invent one: the classifier would bucket it as unknown. "
            f"Add a code to {STRATEGIES_PATH.name} or drop the class from "
            "SEED_PLAN."
        )
    return codes


def _price(rng: random.Random, min_dollars: int, max_dollars: int) -> int:
    """A plausible invoice amount, in integer cents."""
    cents = rng.randint(min_dollars, max_dollars) * 100
    # Roughly a third of real prices end in .95.
    if rng.random() < 0.35 and cents > 500:
        cents -= 5
    return cents


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def _make_customers(rng: random.Random) -> list[Customer]:
    combos = [(suburb, trade) for suburb in SUBURBS for trade in TRADES]
    rng.shuffle(combos)

    customers: list[Customer] = []
    for suburb, (trade, slug) in combos[:CUSTOMER_COUNT]:
        customers.append(
            Customer(
                name=f"{suburb} {trade}",
                email=f"accounts@{_slug(suburb)}{slug}.com.au",
                phone=f"04{rng.randint(10, 99)} {rng.randint(100, 999)} "
                f"{rng.randint(100, 999)}",
                # Thursday/Friday paydays, matching strategies.yaml
                # default_payday_weekdays. Left NULL for roughly a third so
                # the engine's no-history fallback is exercised too.
                observed_payday_weekday=rng.choice([3, 4, 3, 4, None, 0, None]),
            )
        )
    return customers


def _assign_failures(
    rng: random.Random, customers: list[Customer]
) -> list[tuple[Customer, ClassPlan]]:
    """Pick which customers failed, with a few failing repeatedly."""
    selected = rng.sample(customers, FAILING_CUSTOMERS)

    slots: list[Customer] = []
    for customer in selected[:REPEAT_OFFENDERS]:
        slots.extend([customer] * FAILURES_PER_REPEAT)
    slots.extend(selected[REPEAT_OFFENDERS:])

    # Sanity: the arithmetic above must add up to the planned total.
    assert len(slots) == TOTAL_SEEDED, (
        f"{len(slots)} slots for {TOTAL_SEEDED} planned failures — adjust "
        "FAILING_CUSTOMERS / REPEAT_OFFENDERS."
    )

    plans: list[ClassPlan] = []
    for plan in SEED_PLAN:
        plans.extend([plan] * plan.count)

    rng.shuffle(slots)
    rng.shuffle(plans)
    return list(zip(slots, plans))


def _ulid(rng: random.Random) -> str:
    """A ULID-shaped id from the seeded RNG.

    app.core.ids uses os.urandom, which would make the dataset
    non-reproducible. Seeding needs determinism more than entropy.
    """
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "".join(rng.choice(alphabet) for _ in range(26))


def seed_demo(db: Session) -> dict[str, Any]:
    """Reset, then seed the demo dataset. Returns the CONTRACT.md summary."""
    reset_all(db)

    rng = random.Random(SEED)

    customers = _make_customers(rng)
    db.add_all(customers)
    db.flush()

    now = clock.now()
    # (payment_id, failure_class, amount_cents, age_seconds)
    planned: list[tuple[str, str, int, int]] = []

    for customer, plan in _assign_failures(rng, customers):
        amount_cents = _price(rng, plan.min_dollars, plan.max_dollars)
        raw_code = rng.choice(_codes_for(plan.failure_class))

        payment_id = f"pay_{_ulid(rng)}"
        event_id = f"evt_{_ulid(rng)}"
        age_seconds = rng.randint(0, SPREAD_DAYS * 86_400)

        envelope = _build_envelope(
            event_id=event_id,
            payment_id=payment_id,
            customer_id=customer.id,
            amount_cents=amount_cents,
            status=STATUS_FOR_OUTCOME["dishonour"],
            raw_code=raw_code,
            created_at=now - timedelta(seconds=age_seconds),
        )
        db.add(
            SimulatedWebhook(
                event_id=event_id,
                payload=envelope,
                deliver_at=now,
                delivery_number=1,
            )
        )
        planned.append((payment_id, plan.failure_class, amount_cents, age_seconds))

    db.commit()

    # Through the real ingest path — every payment gets a webhook_events row.
    deliver_due(db)

    # Ingest stamps failed_at with clock.now(), which is right for a live
    # webhook but puts all fifty in the same instant. Backdate them here so the
    # ledger has a history. Seed data only; ingest is untouched.
    for payment_id, _, _, age_seconds in planned:
        payment = db.get(Payment, payment_id)
        if payment is not None:
            payment.failed_at = now - timedelta(seconds=age_seconds)
    db.commit()

    return _summarise(planned, len(customers))


def _summarise(
    planned: list[tuple[str, str, int, int]], customer_count: int
) -> dict[str, Any]:
    """Build the /sim/seed-demo response from docs/CONTRACT.md.

    by_class is computed from what was *seeded*, not from payments.failure_class
    — that column is deliberately NULL until Person B's classifier runs. The two
    will agree once it has; disagreeing before then is the honest state.
    """
    by_class: dict[str, dict[str, int]] = {}
    for _, failure_class, amount_cents, _ in planned:
        bucket = by_class.setdefault(failure_class, {"count": 0, "amount_cents": 0})
        bucket["count"] += 1
        bucket["amount_cents"] += amount_cents

    total_cents = sum(entry["amount_cents"] for entry in by_class.values())

    return {
        "seeded": len(planned),
        "customers": customer_count,
        "at_risk_cents": total_cents,
        "by_class": [
            {
                "failure_class": name,
                "count": data["count"],
                "amount_cents": data["amount_cents"],
            }
            # Largest exposure first — the order a merchant would want.
            for name, data in sorted(
                by_class.items(), key=lambda kv: -kv[1]["amount_cents"]
            )
        ],
    }
