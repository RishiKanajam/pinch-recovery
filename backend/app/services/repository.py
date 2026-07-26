"""The engine's persistence seam, over Person A's Postgres tables.

This replaces the in-memory `store.py` Person B built against hand-written
fixtures. Everything above it — the strategy engine, the classifier, the
reasoning generation, the outbox copy, the routers and the templates — is
unchanged: those are pure functions over the `docs/CONTRACT.md` Pydantic shapes
in `app.models.schemas`, and this module's only job is to turn database rows
into those shapes, run the same pure functions, and write the results back.

That split is deliberate. The rules that decide what happens to a dishonour are
the product, and they stay testable with a frozen clock and no database. Only
the storage moved.

Rows in, contract shapes out:

    payments/attempts/customers  ->  schemas.Payment  ->  plan()  ->  rows

One `Repository` is constructed per request around the request's `Session`, so
it inherits the same transaction and commit boundary as the rest of the app
rather than owning a long-lived connection of its own.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core import clock
from app.models import Attempt as AttemptRow
from app.models import Customer as CustomerRow
from app.models import OutboxMessage as OutboxRow
from app.models import Payment as PaymentRow
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
from app.services.classifier import get_strategy_table
from app.services.outbox import render_message
from app.services.strategy_engine import CustomerContext, RecoveryPlan, apply_plan, plan

logger = logging.getLogger(__name__)


@dataclass
class CustomerView:
    """The customer fields the drill-down template reads.

    A view rather than the ORM row because the templates were written against
    Person B's fixture object (`customer.bsb`, `customer.payday_weekday`) while
    the table names those columns `bank_bsb` and `observed_payday_weekday`.
    Renaming either side would break something that currently works, so the
    translation lives here, in one place, at the seam.
    """

    id: str
    name: str
    email: str | None
    bsb: str | None
    account_name: str | None
    payday_weekday: int | None


def _enum_or_none(enum_cls, value):
    """DB strings are free text; a value the enum lost is not worth a 500.

    Columns are plain VARCHAR by design (CLAUDE.md: no ALTER TYPE mid-demo), so
    nothing at the database level stops a stale row holding a class we have
    since renamed. Rendering that as unclassified beats crashing the dashboard.
    """
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        logger.warning("Unknown %s value in database: %r", enum_cls.__name__, value)
        return None


class Repository:
    """Read and write the ledger in `docs/CONTRACT.md` shapes."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def _pinch_client(self):
        """The Pinch boundary, bound to this repository's database.

        The mock resolves against local tables, so it is handed a factory on the
        same bind as this session — left to its own SessionLocal it would settle
        a test's retries against the development database.
        """
        from sqlalchemy.orm import Session as _Session

        from app.services.pinch_client import get_pinch_client

        bind = self.db.get_bind()
        return get_pinch_client(session_factory=lambda: _Session(bind=bind))

    # --- row -> contract shape -------------------------------------------

    @staticmethod
    def _attempt_to_schema(row: AttemptRow) -> Attempt:
        return Attempt(
            id=row.id,
            payment_id=row.payment_id,
            action=ActionType(row.action),
            channel=_enum_or_none(Channel, row.channel),
            status=AttemptStatus(row.status),
            scheduled_for=row.scheduled_for,
            executed_at=row.executed_at,
            attempt_number=row.attempt_number,
            note=row.note,
        )

    @classmethod
    def _payment_to_schema(cls, row: PaymentRow) -> Payment:
        return Payment(
            id=row.id,
            customer_id=row.customer_id,
            customer_name=row.customer.name if row.customer else "",
            amount_cents=row.amount_cents,
            currency=row.currency,
            status=PaymentStatus(row.status),
            raw_code=row.raw_code,
            failure_class=_enum_or_none(FailureClass, row.failure_class),
            failed_at=row.failed_at,
            recovered_at=row.recovered_at,
            attempts=[
                cls._attempt_to_schema(a)
                for a in sorted(row.attempts, key=lambda a: a.attempt_number)
            ],
            reasoning=row.reasoning,
        )

    def _load(self, payment_id: str) -> PaymentRow | None:
        return self.db.execute(
            select(PaymentRow)
            .options(
                selectinload(PaymentRow.attempts), selectinload(PaymentRow.customer)
            )
            .where(PaymentRow.id == payment_id)
        ).scalar_one_or_none()

    def _load_all(self) -> list[PaymentRow]:
        return list(
            self.db.execute(
                select(PaymentRow).options(
                    selectinload(PaymentRow.attempts),
                    selectinload(PaymentRow.customer),
                )
            )
            .scalars()
            .all()
        )

    # --- reads ------------------------------------------------------------

    def list_payments(
        self,
        status: PaymentStatus | None = None,
        failure_class: FailureClass | None = None,
        limit: int = 200,
    ) -> list[Payment]:
        stmt = (
            select(PaymentRow)
            .options(
                selectinload(PaymentRow.attempts), selectinload(PaymentRow.customer)
            )
            # Newest failure first: the dashboard is a worklist, and the thing
            # that just broke is the thing someone wants to see. nulls_last for
            # the same reason as the JSON endpoint — Postgres sorts NULL first
            # under DESC, which would float unfailed payments above real ones.
            .order_by(PaymentRow.failed_at.desc().nulls_last(), PaymentRow.id.desc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(PaymentRow.status == status.value)
        if failure_class is not None:
            stmt = stmt.where(PaymentRow.failure_class == failure_class.value)

        return [self._payment_to_schema(r) for r in self.db.execute(stmt).scalars()]

    def get_payment(self, payment_id: str) -> Payment | None:
        row = self._load(payment_id)
        return self._payment_to_schema(row) if row else None

    def get_customer(self, customer_id: str) -> CustomerView | None:
        row = self.db.get(CustomerRow, customer_id)
        if row is None:
            return None
        return CustomerView(
            id=row.id,
            name=row.name,
            email=row.email,
            bsb=row.bank_bsb,
            account_name=row.bank_account_name,
            payday_weekday=row.observed_payday_weekday,
        )

    def get_plan(self, payment_id: str) -> RecoveryPlan | None:
        """Recompute the decision for the drill-down's "show your working" panel.

        Not cached. `plan()` is pure and measures every offset from the payment's
        `failed_at` rather than from the moment it runs, so recomputing an hour
        later — or after a fast-forward — reproduces the same decision trace
        instead of a second, subtly different one.
        """
        row = self._load(payment_id)
        if row is None or row.failed_at is None:
            return None
        payment = self._payment_to_schema(row)
        return plan(
            payment,
            customer=self._customer_context(payment.customer_id),
            table=get_strategy_table(),
        )

    # --- the customer-level view the global rules need ---------------------

    def _customer_context(self, customer_id: str) -> CustomerContext:
        """Counts only *executed* work.

        A scheduled attempt has not happened yet and must not consume the retry
        budget, or a customer with a full timeline could never be retried again.
        """
        customer = self.db.get(CustomerRow, customer_id)
        rules = get_strategy_table().global_rules
        window_start = clock.now() - timedelta(days=rules.customer_retry_budget_days)

        rows = (
            self.db.execute(
                select(AttemptRow)
                .join(PaymentRow, AttemptRow.payment_id == PaymentRow.id)
                .where(
                    PaymentRow.customer_id == customer_id,
                    AttemptRow.executed_at.is_not(None),
                )
            )
            .scalars()
            .all()
        )

        retries = 0
        last_message_at: datetime | None = None
        for row in rows:
            if row.action == ActionType.RETRY.value:
                if row.executed_at >= window_start:
                    retries += 1
            elif row.channel is not None:
                if last_message_at is None or row.executed_at > last_message_at:
                    last_message_at = row.executed_at

        return CustomerContext(
            customer_id=customer_id,
            payday_weekday=customer.observed_payday_weekday if customer else None,
            retries_in_window=retries,
            last_message_at=last_message_at,
        )

    # --- recovery ---------------------------------------------------------

    # Attempts that have already happened. These are facts, not plans.
    _TERMINAL_STATUSES = (
        AttemptStatus.EXECUTED,
        AttemptStatus.FAILED,
        AttemptStatus.SUCCEEDED,
    )

    def run_recovery(self, payment_id: str) -> Payment | None:
        """Classify and schedule. Idempotent, per docs/CONTRACT.md.

        Idempotency is not free: planning is pure, but a naive implementation
        that overwrites the attempt list deletes the record of every message
        already sent and every retry already presented. You cannot unsend an
        email, so history is preserved and the freshly planned ladder is
        consumed against it.
        """
        row = self._load(payment_id)
        if row is None:
            return None
        if row.failed_at is None:
            # Nothing to classify: a pending payment has not failed yet.
            return self._payment_to_schema(row)

        payment = self._payment_to_schema(row)
        recovery = plan(
            payment,
            customer=self._customer_context(payment.customer_id),
            table=get_strategy_table(),
        )
        updated = apply_plan(payment, recovery)
        merged = self._merge_attempts(payment.attempts, recovery.attempts)

        row.failure_class = updated.failure_class.value if updated.failure_class else None
        row.reasoning = updated.reasoning
        row.status = updated.status.value
        self._persist_attempts(row, merged)

        self.db.commit()
        return self.get_payment(payment_id)

    def classify_unclassified(self) -> int:
        """Run the engine over every failed payment that has not been classified.

        Ingestion deliberately leaves `failure_class` NULL — deriving it is this
        half of the system, and a guess written at ingest would be
        indistinguishable from a real classification. That leaves a gap between
        "50 payments seeded" and "50 payments with a reasoning string", and this
        closes it in one pass.
        """
        rows = (
            self.db.execute(
                select(PaymentRow.id).where(
                    PaymentRow.failure_class.is_(None),
                    PaymentRow.failed_at.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        for payment_id in rows:
            self.run_recovery(payment_id)
        if rows:
            logger.info("Classified %d previously unclassified payment(s)", len(rows))
        return len(rows)

    @classmethod
    def _merge_attempts(cls, existing: list[Attempt], planned: list[Attempt]) -> list[Attempt]:
        """Keep what actually happened; replace only what is still pending.

        One planned step is dropped per already-completed step of the same kind,
        which leaves the remaining tail intact — three of four retries survive if
        one has already run. Without this, calling run-recovery twice also
        produced two different `reasoning` strings, because the customer's
        message history changed underneath the second call.
        """
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

    def _persist_attempts(self, row: PaymentRow, attempts: list[Attempt]) -> None:
        """Sync the planned attempt list onto the payment's rows.

        Upsert by id rather than delete-and-recreate: the ids the engine
        generates are deterministic per payment and position, so a re-plan lands
        on the same rows and the outbox's foreign keys stay pointed at attempts
        that still exist.
        """
        existing = {a.id: a for a in row.attempts}
        keep: set[str] = set()

        for attempt in attempts:
            keep.add(attempt.id)
            target = existing.get(attempt.id)
            if target is None:
                target = AttemptRow(id=attempt.id, payment_id=row.id)
                self.db.add(target)
            target.action = attempt.action.value
            target.channel = attempt.channel.value if attempt.channel else None
            target.status = attempt.status.value
            target.scheduled_for = attempt.scheduled_for
            target.executed_at = attempt.executed_at
            target.attempt_number = attempt.attempt_number
            target.note = attempt.note

        for attempt_id, target in existing.items():
            if attempt_id not in keep:
                self.db.delete(target)

        self.db.flush()

    # --- executing due work ------------------------------------------------

    def execute_due(self) -> int:
        """Execute every attempt whose scheduled time has passed.

        Person A's `/sim/fast-forward` reports due attempts but deliberately does
        not run them — applying retry budgets and max_attempts is this side of
        the seam. This is the other half of that handshake, called by the poller
        and after every fast-forward.

        Idempotent: an attempt only leaves `scheduled` once.
        """
        now = clock.now()
        executed = 0

        due = (
            self.db.execute(
                select(AttemptRow)
                .join(PaymentRow, AttemptRow.payment_id == PaymentRow.id)
                .where(
                    AttemptRow.status == AttemptStatus.SCHEDULED.value,
                    AttemptRow.scheduled_for.is_not(None),
                    AttemptRow.scheduled_for <= now,
                    PaymentRow.status.not_in(
                        (PaymentStatus.RECOVERED.value, PaymentStatus.WRITTEN_OFF.value)
                    ),
                )
                .order_by(AttemptRow.scheduled_for)
            )
            .scalars()
            .all()
        )

        for attempt_row in due:
            payment_row = self._load(attempt_row.payment_id)
            if payment_row is None:
                continue
            # A write-off earlier in this same batch is terminal; anything still
            # scheduled behind it must not also fire.
            if payment_row.status in (
                PaymentStatus.RECOVERED.value,
                PaymentStatus.WRITTEN_OFF.value,
            ):
                continue

            attempt_row.executed_at = attempt_row.scheduled_for
            executed += 1

            if attempt_row.action == ActionType.WRITE_OFF.value:
                attempt_row.status = AttemptStatus.EXECUTED.value
                payment_row.status = PaymentStatus.WRITTEN_OFF.value
                continue

            if attempt_row.action == ActionType.RETRY.value:
                # Present the debit through the Pinch boundary and stop there.
                # A retry does not resolve when it is submitted: the bank
                # answers days later, Pinch reports that answer to
                # /webhooks/pinch, and that is what moves the payment to
                # `recovered`. Deciding the outcome here instead would be the
                # engine marking its own homework, and would leave the ingest
                # path that has to record real recoveries permanently unused.
                #
                # Presented as at the attempt's own scheduled time, so the
                # settlement lands on a date fixed in simulated time rather than
                # one that moves with fast-forward granularity.
                result = self._pinch_client().retry_payment(
                    payment_row.id, presented_at=attempt_row.scheduled_for
                )
                if result.accepted:
                    attempt_row.status = AttemptStatus.EXECUTED.value
                    attempt_row.note = (
                        attempt_row.note or ""
                    ) + " Re-presented via Pinch; awaiting settlement."
                else:
                    attempt_row.status = AttemptStatus.FAILED.value
                    attempt_row.note = (
                        attempt_row.note or ""
                    ) + f" Pinch refused the retry: {result.message}"
                continue

            attempt_row.status = AttemptStatus.EXECUTED.value
            if attempt_row.channel is not None:
                self._send(payment_row, attempt_row)

        if executed:
            self.db.commit()
        return executed

    def _send(self, payment_row: PaymentRow, attempt_row: AttemptRow) -> None:
        """Write the outbox row an executed message attempt implies."""
        payment = self._payment_to_schema(payment_row)
        message = render_message(
            message_id=f"msg_{attempt_row.id.removeprefix('att_')}",
            payment=payment,
            attempt=self._attempt_to_schema(attempt_row),
            failure_class=payment.failure_class or FailureClass.UNKNOWN,
            customer_email=(payment_row.customer.email if payment_row.customer else "")
            or "",
            sent_at=attempt_row.executed_at or clock.now(),
        )
        # The poller can re-enter after a fast-forward burst; an id already
        # present means this attempt's message was written on an earlier pass.
        if self.db.get(OutboxRow, message.id) is not None:
            return
        self.db.add(
            OutboxRow(
                id=message.id,
                payment_id=message.payment_id,
                customer_id=message.customer_id,
                customer_name=message.customer_name,
                channel=message.channel.value,
                action=message.action.value,
                subject=message.subject,
                body=message.body,
                tone=message.tone,
                update_link=message.update_link,
                sent_at=message.sent_at,
                read=False,
            )
        )

    # --- the fake inbox ----------------------------------------------------

    @staticmethod
    def _message_to_schema(row: OutboxRow) -> OutboxMessage:
        return OutboxMessage(
            id=row.id,
            payment_id=row.payment_id,
            customer_id=row.customer_id,
            customer_name=row.customer_name,
            channel=Channel(row.channel),
            subject=row.subject,
            body=row.body,
            sent_at=row.sent_at,
            tone=row.tone,
            action=ActionType(row.action),
            update_link=row.update_link,
            read=row.read,
        )

    def outbox(self, limit: int = 200) -> list[OutboxMessage]:
        rows = (
            self.db.execute(
                select(OutboxRow).order_by(OutboxRow.sent_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )
        return [self._message_to_schema(r) for r in rows]

    def get_message(self, message_id: str) -> OutboxMessage | None:
        row = self.db.get(OutboxRow, message_id)
        return self._message_to_schema(row) if row else None

    def mark_message_read(self, message_id: str) -> None:
        row = self.db.get(OutboxRow, message_id)
        if row is not None and not row.read:
            row.read = True
            self.db.commit()

    def unread_count(self) -> int:
        return len(
            self.db.execute(
                select(OutboxRow.id).where(OutboxRow.read.is_(False))
            )
            .scalars()
            .all()
        )

    # --- dashboard aggregates ----------------------------------------------

    def summary(self) -> DashboardSummary:
        """Aggregates for the top of the dashboard, in integer cents.

        `escalated_cents` is a subset of `at_risk_cents`, not a sibling: a
        payment waiting on a human is still at risk. Presenting them as disjoint
        would make the four figures look like they should sum to the book, and
        they never will.
        """
        rows = self._load_all()
        payments = [self._payment_to_schema(r) for r in rows]

        at_risk = sum(p.amount_cents for p in payments if p.status is PaymentStatus.FAILED)
        recovered = sum(
            p.amount_cents for p in payments if p.status is PaymentStatus.RECOVERED
        )
        written_off = sum(
            p.amount_cents for p in payments if p.status is PaymentStatus.WRITTEN_OFF
        )
        escalated = sum(
            p.amount_cents
            for p in payments
            if p.status is PaymentStatus.FAILED and self._is_escalated(p)
        )

        failed_ever = at_risk + recovered + written_off
        recovery_rate = (recovered / failed_ever) if failed_ever else 0.0

        by_class: list[ClassBreakdown] = []
        for failure_class in FailureClass:
            in_class = [p for p in payments if p.failure_class is failure_class]
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

    # --- the update-details flow -------------------------------------------

    def payment_method(self, customer_id: str) -> PaymentMethod | None:
        row = self.db.get(CustomerRow, customer_id)
        if row is None:
            return None
        last4 = row.bank_account_last4 or ""
        return PaymentMethod(
            customer_id=row.id,
            customer_name=row.name,
            account_name=row.bank_account_name or row.name,
            bsb=row.bank_bsb or "",
            # Only ever the tail. The full number is never stored, so there is
            # nothing here that could leak it.
            account_number_masked="•••• " + last4[-3:] if last4 else "•••• ???",
        )

    def update_payment_method(
        self,
        customer_id: str,
        update: PaymentMethodUpdate,
        payment_id: str | None = None,
    ) -> list[Payment]:
        """Store new details and immediately retry the customer's open failures.

        Returns the payments that recovered. This is the live moment in the demo:
        the customer fixes the cause, so the retry that was futile a second ago
        now succeeds — which is the argument for classifying failures at all.
        """
        customer = self.db.get(CustomerRow, customer_id)
        if customer is None:
            return []

        customer.bank_account_name = update.account_name
        customer.bank_bsb = (
            update.bsb if "-" in update.bsb else f"{update.bsb[:3]}-{update.bsb[3:]}"
        )
        # Only the tail is retained — see the column comment on Customer.
        customer.bank_account_last4 = update.account_number[-4:]
        now = clock.now()
        customer.payment_method_updated_at = now

        candidates = list(
            self.db.execute(
                select(PaymentRow)
                .options(
                    selectinload(PaymentRow.attempts),
                    selectinload(PaymentRow.customer),
                )
                .where(
                    PaymentRow.customer_id == customer_id,
                    PaymentRow.status == PaymentStatus.FAILED.value,
                )
            )
            .scalars()
            .all()
        )
        # The payment the customer arrived from recovers first, so the demo
        # lands on the row the presenter just clicked.
        candidates.sort(key=lambda p: (p.id != payment_id, p.failed_at or now))

        recovered: list[Payment] = []
        for row in candidates:
            # A stopped payment is a dispute, and a cancelled authority is a
            # revoked mandate. New bank details do not grant permission to
            # debit, and auto-charging here is exactly the hostile behaviour the
            # strategy table exists to prevent.
            if row.failure_class in (
                FailureClass.PAYMENT_STOPPED.value,
                FailureClass.AUTHORITY_CANCELLED.value,
            ):
                continue

            pending_retry = next(
                (
                    a
                    for a in row.attempts
                    if a.status == AttemptStatus.SCHEDULED.value
                    and a.action == ActionType.RETRY.value
                ),
                None,
            )
            if pending_retry is not None:
                pending_retry.status = AttemptStatus.SUCCEEDED.value
                pending_retry.executed_at = now
                pending_retry.note = (
                    "Retried immediately after the customer supplied new "
                    "bank details, and succeeded."
                )
            else:
                # No retry was pending (a hard failure), so record the recovery
                # as its own attempt rather than inventing history.
                self.db.add(
                    AttemptRow(
                        id=f"att_{row.id.removeprefix('pay_')}_fix",
                        payment_id=row.id,
                        action=ActionType.RETRY.value,
                        channel=None,
                        status=AttemptStatus.SUCCEEDED.value,
                        scheduled_for=now,
                        executed_at=now,
                        attempt_number=len(row.attempts) + 1,
                        note=(
                            "Details corrected by the customer, so a debit "
                            "against the new account was presented and "
                            "succeeded. No retry was attempted before this "
                            "point — the account was invalid, so a retry "
                            "could only have failed and incurred a fee."
                        ),
                    )
                )

            # Cancel work the recovery made pointless.
            for attempt_row in row.attempts:
                if attempt_row.status == AttemptStatus.SCHEDULED.value:
                    attempt_row.status = AttemptStatus.SKIPPED.value
                    attempt_row.note = (
                        "Cancelled: payment recovered before this was due."
                    )

            row.status = PaymentStatus.RECOVERED.value
            row.recovered_at = now
            recovered.append(row.id)

        self.db.commit()
        return [p for p in (self.get_payment(pid) for pid in recovered) if p]


def get_repository(db: Session) -> Repository:
    """FastAPI-friendly constructor: one repository per request session."""
    return Repository(db)
