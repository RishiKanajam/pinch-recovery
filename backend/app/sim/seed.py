"""The demo dataset: ~50 realistic failed payments across every direct debit
failure class.

Six classes, not seven — expired_card is excluded on purpose, for the reason
documented at EXCLUDED_FROM_SEED below.

This is what the judge looks at, so the data has to read as a real merchant's
ledger rather than as fifty rows of `Test Customer 1 — $100.00`. Names are
Australian service businesses, amounts sit at plausible invoice price points,
and failures are spread across the last three weeks so the dashboard has a
shape instead of a spike.

Deterministic by design: one fixed RNG seed, so a rehearsal and the live run
produce the same numbers. Row ids still differ between runs — they are ULIDs
containing a timestamp — but every count and every total is reproducible.

Everything is seeded through the real webhook ingest path, so each payment has
a genuine webhook_events row behind it. Nothing here writes a payment directly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core import clock
from app.models import Customer, Payment, SimulatedWebhook
from app.sim.service import _build_envelope, deliver_due, reset_all

# Bumping this changes the whole dataset. Don't, mid-rehearsal.
SEED = 20260725

# Australian service businesses — the kind of merchant that lives on direct
# debit and feels every dishonour.
BUSINESSES: tuple[tuple[str, str], ...] = (
    ("Marina Auto Detailing", "accounts@marinadetailing.com.au"),
    ("Bondi Beach Physio", "billing@bondibeachphysio.com.au"),
    ("Fremantle Plumbing & Gas", "office@fremantleplumbing.com.au"),
    ("Northside Dog Grooming", "hello@northsidedogs.com.au"),
    ("Toowong Dental Studio", "accounts@toowongdental.com.au"),
    ("Glenelg Surf School", "bookings@glenelgsurf.com.au"),
    ("Carlton Pilates Collective", "studio@carltonpilates.com.au"),
    ("Redfern Barber Co", "shop@redfernbarber.com.au"),
    ("Manly Boat Maintenance", "service@manlyboat.com.au"),
    ("Subiaco Skin Clinic", "reception@subiacoskin.com.au"),
    ("Hobart Hills Landscaping", "quotes@hobarthills.com.au"),
    ("Newtown Music Tuition", "admin@newtownmusic.com.au"),
    ("Parramatta Pest Control", "bookings@parrapest.com.au"),
    ("Geelong Mobile Mechanics", "jobs@geelongmobile.com.au"),
    ("Coogee Cleaning Crew", "accounts@coogeeclean.com.au"),
    ("Adelaide Hills Yoga", "hello@adelaidehillsyoga.com.au"),
)


@dataclass(frozen=True)
class ClassPlan:
    """How many failures of one class to seed, and what they look like."""

    failure_class: str
    raw_codes: tuple[str, ...]
    count: int
    # Inclusive dollar range; converted to integer cents.
    min_dollars: int
    max_dollars: int


# Weighted to look like reality: insufficient funds dominates a real
# dishonour ledger, deliberate cancellations are rare, disputes rarer still.
#
# raw_codes are Pinch's real strings (hyphenated lowercase), per
# docs/pinch-codes-proposal.md. Two classes have NO Pinch code and carry
# simulator-only strings — Pinch cannot produce them. If Person B decides
# differently on that proposal, this table is the single place to change.
SEED_PLAN: tuple[ClassPlan, ...] = (
    ClassPlan("insufficient_funds", ("insufficient-funds",), 20, 49, 890),
    ClassPlan("invalid_account", ("invalid-account",), 9, 120, 2490),
    ClassPlan("technical", ("technical-error", "temporary-problem"), 6, 49, 990),
    ClassPlan("do_not_honour", ("blocked-by-bank",), 6, 99, 1290),
    # SIMULATOR-ONLY below: no corresponding Pinch dishonour code exists.
    ClassPlan("authority_cancelled", ("authority-cancelled",), 6, 99, 1590),
    ClassPlan("payment_stopped", ("payment-stopped",), 3, 249, 1890),
)

# expired_card is deliberately absent.
#
# A card cannot expire on a direct debit — invalid-card and unsupported-card are
# card-scheme failures, and this is a direct debit recovery product. Seeding it
# puts a class on screen that invites "why is there a card expiry in a direct
# debit demo?", which is a credibility question, not a feature. Its four
# payments were reallocated to the classes that are real for direct debit.
#
# The class stays in strategies.yaml: the engine handling it shows range, and a
# card-funded subscription would legitimately produce it. It is the demo
# dataset it does not belong in.
EXCLUDED_FROM_SEED = ("expired_card",)

TOTAL_SEEDED = sum(plan.count for plan in SEED_PLAN)

# Failures spread across the write-off horizon from strategies.yaml, so the
# dashboard shows a range of ages rather than fifty simultaneous failures.
SPREAD_DAYS = 20


def _price(rng: random.Random, min_dollars: int, max_dollars: int) -> int:
    """A plausible invoice amount, in integer cents."""
    cents = rng.randint(min_dollars, max_dollars) * 100
    # Roughly a third of real prices end in .95.
    if rng.random() < 0.35 and cents > 500:
        cents -= 5
    return cents


def seed_demo(db: Session) -> dict[str, Any]:
    """Reset, then seed the demo dataset. Returns the CONTRACT.md summary."""
    reset_all(db)

    rng = random.Random(SEED)

    customers: list[Customer] = []
    for name, email in BUSINESSES:
        customers.append(
            Customer(
                name=name,
                email=email,
                phone=f"04{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(100, 999)}",
                # Thursday/Friday paydays, matching
                # strategies.yaml default_payday_weekdays. Left NULL for some
                # so the engine's fallback path is exercised too.
                observed_payday_weekday=rng.choice([3, 4, None, 3, None]),
            )
        )
    db.add_all(customers)
    db.flush()

    now = clock.now()
    # (payment_id, failure_class, amount_cents, age_seconds)
    planned: list[tuple[str, str, int, int]] = []

    for plan in SEED_PLAN:
        for _ in range(plan.count):
            customer = rng.choice(customers)
            amount_cents = _price(rng, plan.min_dollars, plan.max_dollars)
            raw_code = rng.choice(plan.raw_codes)

            payment_id = f"pay_{_ulid(rng)}"
            event_id = f"evt_{_ulid(rng)}"
            age_seconds = rng.randint(0, SPREAD_DAYS * 86_400)

            envelope = _build_envelope(
                event_id=event_id,
                event_type="payment.dishonoured",
                payment_id=payment_id,
                customer_id=customer.id,
                amount_cents=amount_cents,
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

    # Ingest stamps failed_at with clock.now(), which is correct for a live
    # webhook but makes all fifty land in the same instant. Backdate them here
    # so the ledger has a history. Seed data only; ingest is untouched.
    for payment_id, _, _, age_seconds in planned:
        payment = db.get(Payment, payment_id)
        if payment is not None:
            payment.failed_at = now - timedelta(seconds=age_seconds)
    db.commit()

    return _summarise(planned, len(customers))


def _ulid(rng: random.Random) -> str:
    """A ULID-shaped id drawn from the seeded RNG.

    app.core.ids uses os.urandom, which would make the dataset
    non-reproducible. Seeding needs determinism more than it needs entropy.
    """
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "".join(rng.choice(alphabet) for _ in range(26))


def _summarise(
    planned: list[tuple[str, str, int, int]], customer_count: int
) -> dict[str, Any]:
    """Build the /sim/seed-demo response from docs/CONTRACT.md.

    by_class is computed from what was *seeded*, not from payments.failure_class
    — that column is deliberately NULL until Person B's classifier runs. The
    two will agree once it has, and disagreeing before then is the point: this
    summary says what we generated, the dashboard says what was classified.
    """
    by_class: dict[str, dict[str, int]] = {}
    for _, failure_class, amount_cents, _ in planned:
        bucket = by_class.setdefault(
            failure_class, {"count": 0, "amount_cents": 0}
        )
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
