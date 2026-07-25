"""In-memory store standing in for Person A's Postgres tables.

This is the seam. Everything above it (routers, templates, engine) reads through
the methods on `Store`, so when Person A's repository lands, this module is
replaced and nothing else changes. Nothing here is meant to survive the
hackathon — hence dicts and a lock rather than a session factory.

The store also plays the role of the executor: it advances attempts whose time
has arrived, writes the outbox message an attempt implies, and applies recovery
outcomes. In the real system that work is split between Person A's Pinch client
and a worker; keeping it in one place here means the demo is driven by one call.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from app.core import clock
from app.models.enums import (
    ActionType,
    AttemptStatus,
    Channel,
    FailureClass,
    PaymentStatus,
)
from app.models.schemas import (
    Attempt,
    ClassBreakdown,
    DashboardSummary,
    OutboxMessage,
    Payment,
    PaymentMethod,
    PaymentMethodUpdate,
)
from app.services import fixtures
from app.services.classifier import get_strategy_table
from app.services.outbox import render_message
from app.services.strategy_engine import CustomerContext, RecoveryPlan, apply_plan, plan

logger = logging.getLogger(__name__)

# Fixed seed: /sim/reset must produce the same dashboard every time or the demo
# script stops matching what is on screen halfway through a rehearsal.
SEED = 20260725


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._customers: dict[str, fixtures.RawCustomer] = {}
        self._payments: dict[str, Payment] = {}
        self._plans: dict[str, RecoveryPlan] = {}
        self._outbox: list[OutboxMessage] = []
        self._message_counter = 0
        self.seed()

    # --- seeding ----------------------------------------------------------

    def seed(self) -> None:
        """Build the demo dataset and run the engine over all of it."""
        import random

        with self._lock:
            rng = random.Random(SEED)
            self._customers = {}
            self._payments = {}
            self._plans = {}
            self._outbox = []
            self._message_counter = 0

            customers = fixtures.build_customers(rng)
            for customer in customers:
                self._customers[customer.id] = customer

            raw_payments = fixtures.build_payments(rng, customers)
            now = clock.now()

            for raw in raw_payments:
                customer = self._customers[raw.customer_id]
                payment = Payment(
                    id=raw.id,
                    customer_id=raw.customer_id,
                    customer_name=customer.name,
                    amount_cents=raw.amount_cents,
                    currency="AUD",
                    status=PaymentStatus.FAILED,
                    raw_code=raw.raw_code,
                    failed_at=now - timedelta(hours=raw.failed_hours_ago),
                )
                self._payments[payment.id] = payment
                self._run_recovery_locked(payment.id)
                self._apply_seeded_outcome_locked(raw)

            # Anything already due at seed time is executed, so the dashboard
            # opens on a system that has visibly been working rather than one
            # with fifty pristine future timelines.
            self._execute_due_locked()
            logger.info(
                "Seeded %d payments across %d customers",
                len(self._payments),
                len(self._customers),
            )

    def reset(self) -> None:
        """POST /sim/reset — clock back to real time, dataset back to known."""
        clock.reset()
        self.seed()

    def fast_forward(self, seconds: float) -> datetime:
        """Advance the simulated clock, then run whatever that made due."""
        new_now = clock.fast_forward(seconds)
        with self._lock:
            self._execute_due_locked()
        return new_now

    # --- recovery ---------------------------------------------------------

    def run_recovery(self, payment_id: str) -> Payment | None:
        with self._lock:
            return self._run_recovery_locked(payment_id)

    # Attempts that have already happened. These are facts, not plans.
    _TERMINAL_STATUSES = (
        AttemptStatus.EXECUTED,
        AttemptStatus.FAILED,
        AttemptStatus.SUCCEEDED,
    )

    def _run_recovery_locked(self, payment_id: str) -> Payment | None:
        payment = self._payments.get(payment_id)
        if payment is None:
            return None

        recovery = plan(
            payment,
            customer=self._customer_context_locked(payment.customer_id),
            table=get_strategy_table(),
        )
        updated = apply_plan(payment, recovery)
        updated = updated.model_copy(
            update={"attempts": self._merge_attempts(payment.attempts, recovery.attempts)}
        )
        self._payments[payment_id] = updated
        self._plans[payment_id] = recovery
        return updated

    @classmethod
    def _merge_attempts(cls, existing: list[Attempt], planned: list[Attempt]) -> list[Attempt]:
        """Keep what actually happened; replace only what is still pending.

        `POST /payments/{id}/run-recovery` is specified as idempotent, and a
        naive implementation that overwrites the attempt list is not: it deletes
        the record of every message already sent and every retry already
        presented. You cannot unsend an email, so that history is preserved and
        the freshly planned ladder is consumed against it — one planned step
        dropped per already-completed step of the same kind, which leaves the
        remaining tail intact (three of four retries survive if one has run).

        Without this, calling run-recovery twice also produced two different
        `reasoning` strings, because the customer's message history changed
        underneath the second call.
        """
        from collections import Counter

        history = [a for a in existing if a.status in cls._TERMINAL_STATUSES]
        already_done = Counter((a.action, a.channel) for a in history)

        merged = list(history)
        for attempt in planned:
            key = (attempt.action, attempt.channel)
            if already_done[key] > 0:
                already_done[key] -= 1
                continue
            merged.append(attempt)

        merged.sort(
            key=lambda a: (
                a.scheduled_for is None,
                a.scheduled_for or a.executed_at or clock.now(),
            )
        )
        for i, attempt in enumerate(merged, start=1):
            attempt.attempt_number = i
        return merged

    def _customer_context_locked(self, customer_id: str) -> CustomerContext:
        """Build the per-customer view the global rules need.

        Counts only *executed* retries and *executed* messages: a scheduled
        attempt has not happened yet and must not consume the budget, or a
        customer with a full timeline could never be retried again.
        """
        raw = self._customers.get(customer_id)
        rules = get_strategy_table().global_rules
        window_start = clock.now() - timedelta(days=rules.customer_retry_budget_days)

        retries = 0
        last_message_at: datetime | None = None

        for payment in self._payments.values():
            if payment.customer_id != customer_id:
                continue
            for attempt in payment.attempts:
                if attempt.executed_at is None:
                    continue
                if attempt.action is ActionType.RETRY:
                    if attempt.executed_at >= window_start:
                        retries += 1
                elif attempt.channel is not None:
                    if last_message_at is None or attempt.executed_at > last_message_at:
                        last_message_at = attempt.executed_at

        return CustomerContext(
            customer_id=customer_id,
            payday_weekday=raw.payday_weekday if raw else None,
            retries_in_window=retries,
            last_message_at=last_message_at,
        )

    # --- executing due work ----------------------------------------------

    def execute_due(self) -> int:
        with self._lock:
            return self._execute_due_locked()

    def _execute_due_locked(self) -> int:
        """Execute every attempt whose scheduled time has passed.

        Called after seeding and after every fast-forward, and by the poller.
        Idempotent: an attempt only leaves `scheduled` once.
        """
        now = clock.now()
        executed = 0

        for payment_id, payment in list(self._payments.items()):
            if payment.status in (PaymentStatus.RECOVERED, PaymentStatus.WRITTEN_OFF):
                continue

            for attempt in payment.attempts:
                if attempt.status is not AttemptStatus.SCHEDULED:
                    continue
                if attempt.scheduled_for is None or attempt.scheduled_for > now:
                    continue

                attempt.executed_at = attempt.scheduled_for
                executed += 1

                if attempt.action is ActionType.WRITE_OFF:
                    attempt.status = AttemptStatus.EXECUTED
                    self._payments[payment_id] = payment.model_copy(
                        update={"status": PaymentStatus.WRITTEN_OFF}
                    )
                    payment = self._payments[payment_id]
                    break

                if attempt.action is ActionType.RETRY:
                    # A retry against unchanged details fails again — that is the
                    # whole point of the hard-failure rule. Recovery in the demo
                    # comes from the customer updating details, not from luck.
                    attempt.status = AttemptStatus.FAILED
                    attempt.note = (attempt.note or "") + " Retry presented and failed again."
                    continue

                attempt.status = AttemptStatus.EXECUTED
                if attempt.channel is not None:
                    self._send_locked(payment, attempt)

        return executed

    def _send_locked(self, payment: Payment, attempt: Attempt) -> None:
        """Write the outbox row an executed message attempt implies."""
        self._message_counter += 1
        customer = self._customers.get(payment.customer_id)
        message = render_message(
            message_id=f"msg_01HX{self._message_counter:04d}",
            payment=payment,
            attempt=attempt,
            failure_class=payment.failure_class or FailureClass.UNKNOWN,
            customer_email=customer.email if customer else "",
            sent_at=attempt.executed_at or clock.now(),
        )
        self._outbox.append(message)

    def _apply_seeded_outcome_locked(self, raw: fixtures.RawPayment) -> None:
        """Give a seeded payment its terminal outcome, if it has one.

        Seeded history only — the dashboard needs recovered and written-off
        money on first load, not four zeroes. Live recovery during the demo goes
        through update_payment_method instead.
        """
        if raw.seeded_outcome is None:
            return

        payment = self._payments[raw.id]
        n = max(1, raw.attempts_before_outcome)
        touched = 0

        for attempt in payment.attempts:
            if touched >= n:
                break
            if attempt.status is not AttemptStatus.SCHEDULED:
                continue
            if attempt.action is ActionType.WRITE_OFF:
                continue
            attempt.executed_at = attempt.scheduled_for
            attempt.status = (
                AttemptStatus.FAILED
                if attempt.action is ActionType.RETRY
                else AttemptStatus.EXECUTED
            )
            if attempt.channel is not None:
                self._send_locked(payment, attempt)
            touched += 1

        if raw.seeded_outcome == "recovered":
            recovered_at = payment.failed_at + timedelta(
                hours=raw.attempts_before_outcome * 30 + 6
            )
            # A recovery in the future would render as a payment that recovered
            # before it was recovered. Clamp to now.
            recovered_at = min(recovered_at, clock.now())
            for attempt in payment.attempts:
                if (
                    attempt.action is ActionType.RETRY
                    and attempt.status is AttemptStatus.FAILED
                ):
                    attempt.status = AttemptStatus.SUCCEEDED
                    attempt.note = (attempt.note or "") + " Recovered on this attempt."
                    break
            self._payments[raw.id] = payment.model_copy(
                update={
                    "status": PaymentStatus.RECOVERED,
                    "recovered_at": recovered_at,
                }
            )
        elif raw.seeded_outcome == "written_off":
            self._payments[raw.id] = payment.model_copy(
                update={"status": PaymentStatus.WRITTEN_OFF}
            )

    # --- reads ------------------------------------------------------------

    def list_payments(
        self,
        status: PaymentStatus | None = None,
        failure_class: FailureClass | None = None,
        limit: int = 200,
    ) -> list[Payment]:
        with self._lock:
            rows = list(self._payments.values())

        if status is not None:
            rows = [p for p in rows if p.status is status]
        if failure_class is not None:
            rows = [p for p in rows if p.failure_class is failure_class]

        # Most recent failure first: the dashboard is a worklist, and the thing
        # that just broke is the thing someone wants to see.
        rows.sort(key=lambda p: p.failed_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows[:limit]

    def get_payment(self, payment_id: str) -> Payment | None:
        with self._lock:
            return self._payments.get(payment_id)

    def get_plan(self, payment_id: str) -> RecoveryPlan | None:
        with self._lock:
            return self._plans.get(payment_id)

    def get_customer(self, customer_id: str) -> fixtures.RawCustomer | None:
        with self._lock:
            return self._customers.get(customer_id)

    def summary(self) -> DashboardSummary:
        """Aggregates for the top of the dashboard, in integer cents.

        `escalated_cents` is a subset of `at_risk_cents`, not a sibling: a
        payment waiting on a human is still at risk. Presenting them as disjoint
        would make the four figures look like they should sum to the book, and
        they never will.
        """
        with self._lock:
            rows = list(self._payments.values())

        at_risk = sum(p.amount_cents for p in rows if p.status is PaymentStatus.FAILED)
        recovered = sum(
            p.amount_cents for p in rows if p.status is PaymentStatus.RECOVERED
        )
        written_off = sum(
            p.amount_cents for p in rows if p.status is PaymentStatus.WRITTEN_OFF
        )
        escalated = sum(
            p.amount_cents
            for p in rows
            if p.status is PaymentStatus.FAILED and self._is_escalated(p)
        )

        failed_ever = at_risk + recovered + written_off
        recovery_rate = (recovered / failed_ever) if failed_ever else 0.0

        by_class: list[ClassBreakdown] = []
        for failure_class in FailureClass:
            in_class = [p for p in rows if p.failure_class is failure_class]
            if not in_class:
                continue
            by_class.append(
                ClassBreakdown(
                    failure_class=failure_class,
                    count=len(in_class),
                    amount_cents=sum(p.amount_cents for p in in_class),
                    recovered_cents=sum(
                        p.amount_cents
                        for p in in_class
                        if p.status is PaymentStatus.RECOVERED
                    ),
                )
            )
        by_class.sort(key=lambda c: c.amount_cents, reverse=True)

        return DashboardSummary(
            at_risk_cents=at_risk,
            recovered_cents=recovered,
            escalated_cents=escalated,
            written_off_cents=written_off,
            recovery_rate=round(recovery_rate, 4),
            by_class=by_class,
        )

    @staticmethod
    def _is_escalated(payment: Payment) -> bool:
        return any(
            a.action is ActionType.NOTIFY_HUMAN
            and a.status in (AttemptStatus.EXECUTED, AttemptStatus.SCHEDULED)
            for a in payment.attempts
        )

    def outbox(self, limit: int = 200) -> list[OutboxMessage]:
        with self._lock:
            rows = list(self._outbox)
        rows.sort(key=lambda m: m.sent_at, reverse=True)
        return rows[:limit]

    def get_message(self, message_id: str) -> OutboxMessage | None:
        with self._lock:
            for message in self._outbox:
                if message.id == message_id:
                    return message
        return None

    def mark_message_read(self, message_id: str) -> None:
        with self._lock:
            for message in self._outbox:
                if message.id == message_id:
                    message.read = True
                    return

    # --- the update-details flow -----------------------------------------

    def payment_method(self, customer_id: str) -> PaymentMethod | None:
        with self._lock:
            customer = self._customers.get(customer_id)
        if customer is None:
            return None
        return PaymentMethod(
            customer_id=customer.id,
            customer_name=customer.name,
            account_name=customer.account_name,
            bsb=customer.bsb,
            # Only ever the tail. Full numbers never leave the store.
            account_number_masked="•••• " + customer.account_number[-3:],
        )

    def update_payment_method(
        self, customer_id: str, update: PaymentMethodUpdate, payment_id: str | None = None
    ) -> list[Payment]:
        """Store new details and immediately retry the customer's open failures.

        Returns the payments that recovered. This is the live moment in the demo:
        the customer fixes the cause, so the retry that was futile a second ago
        now succeeds — which is the argument for classifying failures at all.
        """
        with self._lock:
            customer = self._customers.get(customer_id)
            if customer is None:
                return []

            customer.account_name = update.account_name
            customer.bsb = update.bsb if "-" in update.bsb else f"{update.bsb[:3]}-{update.bsb[3:]}"
            customer.account_number = update.account_number

            now = clock.now()
            recovered: list[Payment] = []

            candidates = [
                p
                for p in self._payments.values()
                if p.customer_id == customer_id and p.status is PaymentStatus.FAILED
            ]
            # The payment the customer arrived from recovers first, so the demo
            # lands on the row the presenter just clicked.
            candidates.sort(key=lambda p: (p.id != payment_id, p.failed_at or now))

            for payment in candidates:
                # A stopped payment is a dispute. New bank details do not grant
                # permission to debit, and auto-charging here is exactly the
                # hostile behaviour the strategy table exists to prevent.
                if payment.failure_class is FailureClass.PAYMENT_STOPPED:
                    continue
                if payment.failure_class is FailureClass.AUTHORITY_CANCELLED:
                    continue

                for attempt in payment.attempts:
                    if attempt.status is AttemptStatus.SCHEDULED and (
                        attempt.action is ActionType.RETRY
                    ):
                        attempt.status = AttemptStatus.SUCCEEDED
                        attempt.executed_at = now
                        attempt.note = (
                            "Retried immediately after the customer supplied new "
                            "bank details, and succeeded."
                        )
                        break
                else:
                    # No retry was pending (a hard failure), so record the
                    # recovery as its own attempt rather than inventing history.
                    payment.attempts.append(
                        Attempt(
                            id=f"att_{payment.id.removeprefix('pay_')}_fix",
                            payment_id=payment.id,
                            action=ActionType.RETRY,
                            channel=None,
                            status=AttemptStatus.SUCCEEDED,
                            scheduled_for=now,
                            executed_at=now,
                            attempt_number=len(payment.attempts) + 1,
                            note=(
                                "Details corrected by the customer, so a debit "
                                "against the new account was presented and "
                                "succeeded. No retry was attempted before this "
                                "point — the account was invalid, so a retry "
                                "could only have failed and incurred a fee."
                            ),
                        )
                    )

                # Cancel work that the recovery made pointless.
                for attempt in payment.attempts:
                    if attempt.status is AttemptStatus.SCHEDULED:
                        attempt.status = AttemptStatus.SKIPPED
                        attempt.note = "Cancelled: payment recovered before this was due."

                updated = payment.model_copy(
                    update={
                        "status": PaymentStatus.RECOVERED,
                        "recovered_at": now,
                    }
                )
                self._payments[payment.id] = updated
                recovered.append(updated)

            return recovered


_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store:
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store
