"""The strategy engine: classified payment -> ordered, scheduled attempts.

Reading order for this file: `plan()` is the whole story, everything else is a
rule it calls out to. The rules are the product; the sequencing around them is
plumbing.

Two modelling decisions worth stating because they are not obvious from the
YAML alone:

1. `delay_hours` is an offset from the moment of classification, not from the
   previous action. The invalid_account ladder (0h email, 48h SMS, 96h human) is
   "escalating channel over four days" as its reasoning says — reading those as
   cumulative would stretch it to six.

2. A `retry` action is *repeated* up to `max_attempts` times, spaced by its own
   `delay_hours`. That is why insufficient_funds lists one retry action but has
   max_attempts: 4 — four retries on successive paydays, not four different
   actions someone forgot to write down.

A suppressed action becomes an Attempt with status `skipped` and a note saying
why, rather than vanishing. The drill-down renders those, and "we deliberately
did not do this, here's the reason" is a stronger demo than a gap in a timeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.core import clock
from app.models.enums import (
    HARD_FAILURE_CLASSES,
    ActionType,
    AttemptStatus,
    Channel,
    FailureClass,
    PaymentStatus,
)
from app.models.schemas import Attempt, Payment, Strategy
from app.services.classifier import GlobalRules, StrategyTable, get_strategy_table
from app.services.scheduler import (
    business_day,
    hours_from,
    next_month_equivalent,
    next_payday,
)

logger = logging.getLogger(__name__)

# Actions that constitute contacting the customer. `notify_human` is internal
# (it pages the account owner, not the customer) and a silent retry is invisible
# by design, so neither counts against the message-frequency cap.
CUSTOMER_FACING_ACTIONS = frozenset(
    {ActionType.REQUEST_DETAILS_UPDATE, ActionType.SAVE_OFFER, ActionType.OFFER_SPLIT}
)


def looks_monthly(payment: Payment, rules: GlobalRules) -> bool:
    """Whether this payment reads as a monthly bill rather than a weekly one.

    Amount is the only signal available on a first dishonour, and it is a
    decent one: a $39 debit is a weekly or fortnightly service, a $340 one is
    somebody's monthly invoice. It is a heuristic and the reasoning string says
    so — nothing here is learned from history, and claiming otherwise is the
    overstatement docs/CONTRACT.md and the pitch both refuse to make.
    """
    return payment.amount_cents >= rules.monthly_payer_min_cents


@dataclass
class CustomerContext:
    """What the engine needs to know about the customer, not just the payment.

    The retry budget and the message cap are per *customer* — three failures
    across two invoices is one relationship problem, per the global_rules
    comment in strategies.yaml. Without this object the engine would happily
    send the same person four emails in a morning about four invoices.
    """

    customer_id: str
    # Observed payday from history, Mon=0. None falls back to the global default.
    payday_weekday: int | None = None
    # Retries already made for this customer inside customer_retry_budget_days.
    retries_in_window: int = 0
    # Last time we contacted this customer on any channel.
    last_message_at: datetime | None = None

    def payday_weekdays(self, rules: GlobalRules) -> list[int]:
        if self.payday_weekday is not None:
            return [self.payday_weekday]
        return list(rules.default_payday_weekdays)

    @property
    def has_observed_payday(self) -> bool:
        return self.payday_weekday is not None


@dataclass
class RecoveryPlan:
    """The engine's full decision for one payment."""

    payment_id: str
    failure_class: FailureClass
    strategy: Strategy
    attempts: list[Attempt]
    reasoning: str
    notify_human: bool
    write_off_at: datetime | None
    # Ordered, human-readable record of how the decision was reached. Rendered
    # verbatim in the drill-down — this is the "show your working" panel.
    decision_trace: list[str] = field(default_factory=list)

    @property
    def scheduled_attempts(self) -> list[Attempt]:
        return [a for a in self.attempts if a.status is AttemptStatus.SCHEDULED]

    @property
    def skipped_attempts(self) -> list[Attempt]:
        return [a for a in self.attempts if a.status is AttemptStatus.SKIPPED]

    @property
    def retry_count(self) -> int:
        return len([a for a in self.scheduled_attempts if a.action is ActionType.RETRY])


def _attempt_id(payment_id: str, n: int) -> str:
    return f"att_{payment_id.removeprefix('pay_')}_{n:02d}"


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def split_halves(amount_cents: int) -> tuple[int, int]:
    """Split an amount into two instalments that add back to it exactly.

    Integer cents, so an odd amount puts the extra cent on the first
    instalment. Money never becomes a float here, and the two halves summing
    to the original is the property that keeps the ledger honest.
    """
    first = (amount_cents + 1) // 2
    return first, amount_cents - first


def plan(
    payment: Payment,
    customer: CustomerContext | None = None,
    table: StrategyTable | None = None,
    now: datetime | None = None,
) -> RecoveryPlan:
    """Classify `payment` and produce its scheduled attempts.

    Pure: reads the strategy table and the customer context, returns a plan.
    Nothing is persisted and nothing is sent — the caller decides that. Keeping
    it pure is what makes the rules testable with a frozen clock.
    """
    table = table or get_strategy_table()
    rules = table.global_rules
    customer = customer or CustomerContext(customer_id=payment.customer_id)

    # Every offset — actions *and* the write-off horizon — is measured from the
    # failure, not from whenever this function happens to run. In production the
    # two are the same instant (the webhook lands seconds after the dishonour),
    # but they diverge for any backdated payment, and measuring actions from
    # "now" produces a timeline that reads as nonsense: a payment that failed on
    # Tuesday gets its "we couldn't collect your payment" email on Friday while
    # its write-off date is still counted from Tuesday. Anchoring both to
    # failed_at also makes re-running recovery idempotent in time — it cannot
    # quietly extend a payment's life by restarting the ladder.
    at = now or payment.failed_at or clock.now()

    failure_class = table.classify(payment.raw_code)
    strategy = table.strategy_for(failure_class)
    trace: list[str] = [
        f"Dishonour code {payment.raw_code or '(none)'} classified as "
        f"{failure_class.value}."
        if payment.raw_code
        else "No dishonour code supplied; classified as unknown."
    ]
    if failure_class is FailureClass.UNKNOWN and payment.raw_code:
        trace.append(
            f"Code {payment.raw_code!r} is not in the strategy table. Treated "
            "conservatively and flagged for a human to extend the mapping."
        )

    attempts: list[Attempt] = []
    counter = 0

    def add(
        action: ActionType,
        scheduled_for: datetime | None,
        note: str,
        channel: Channel | None = None,
        status: AttemptStatus = AttemptStatus.SCHEDULED,
    ) -> Attempt:
        nonlocal counter
        counter += 1
        attempt = Attempt(
            id=_attempt_id(payment.id, counter),
            payment_id=payment.id,
            action=action,
            channel=channel,
            status=status,
            scheduled_for=scheduled_for,
            attempt_number=counter,
            note=note,
        )
        attempts.append(attempt)
        return attempt

    # --- retries ------------------------------------------------------------
    retry_actions = [a for a in strategy.actions if a.action is ActionType.RETRY]
    retry_budget_left = max(
        0, rules.customer_max_retries_in_window - customer.retries_in_window
    )

    if failure_class in HARD_FAILURE_CLASSES:
        # Belt and braces: the loader already rejects a hard class with retries,
        # but this is rule 5 and it is cheaper to assert it twice than to explain
        # a dishonour fee to a judge.
        if retry_actions:
            logger.error(
                "Hard failure %s carried retry actions; dropping them",
                failure_class.value,
            )
        retry_actions = []
        trace.append(
            "Hard failure — a retry cannot succeed here and each attempt costs a "
            "dishonour fee, so zero retries are scheduled."
        )
    elif strategy.max_attempts == 0:
        retry_actions = []
        trace.append("Strategy allows no retries for this class.")
    elif retry_budget_left == 0 and retry_actions:
        for action in retry_actions:
            add(
                ActionType.RETRY,
                None,
                (
                    f"Skipped: customer already used "
                    f"{customer.retries_in_window} of "
                    f"{rules.customer_max_retries_in_window} retries in the last "
                    f"{rules.customer_retry_budget_days} days."
                ),
                status=AttemptStatus.SKIPPED,
            )
        retry_actions = []
        trace.append(
            f"Retry budget exhausted for this customer "
            f"({customer.retries_in_window}/"
            f"{rules.customer_max_retries_in_window} in "
            f"{rules.customer_retry_budget_days} days). Retries suppressed — the "
            "problem is the relationship, not this invoice."
        )

    # --- write-off horizon --------------------------------------------------
    # Computed before the retries because it caps them, and it depends on
    # whether this ladder will step out to a monthly date: a monthly payer's
    # next realistic chance to pay is their next billing date, and closing the
    # file at 21 days would write the payment off days before the only attempt
    # likely to work. The extension applies only when that attempt is actually
    # scheduled — it is not a longer horizon for large amounts in general.
    repeats_available = min(strategy.max_attempts, retry_budget_left)
    monthly_payer = looks_monthly(payment, rules)
    monthly_escalates = (
        monthly_payer
        and repeats_available >= rules.monthly_escalation_attempt
        and any(a.align_to_payday for a in retry_actions)
    )
    write_off_days = (
        rules.monthly_payer_write_off_days
        if monthly_escalates
        else rules.write_off_after_days
    )
    write_off_at = hours_from(at, write_off_days * 24)

    for action in retry_actions:
        repeats = repeats_available
        # Repeats are spaced from the *previous retry*, not from classification.
        # Measuring every repeat from t0 looks equivalent but is not: once
        # payday alignment moves retry 1 forward to Thursday, a retry 2 measured
        # from t0 aligns to the very next Thursday-or-Friday and lands a day
        # later, so "four retries on successive paydays" collapses into two
        # retries on consecutive mornings. Chaining keeps the interval real.
        previous = at
        stepped_monthly = False
        for i in range(repeats):
            number = i + 1
            note_parts = []

            if monthly_escalates and number >= rules.monthly_escalation_attempt:
                # Two payday attempts have already failed, which is evidence the
                # problem is not "wrong week" — it is that this customer's money
                # arrives monthly. Stepping to their next equivalent date beats a
                # third attempt on the same weekly rhythm that just failed twice.
                target = next_month_equivalent(previous if stepped_monthly else at)
                stepped_monthly = True
                note_parts.append(
                    "Stepped to next month's equivalent date — the amount reads "
                    "as a monthly bill, so the next real chance to collect is "
                    "their next billing date, not another weekly payday."
                )
            else:
                target = hours_from(previous, action.delay_hours)
                if action.align_to_payday:
                    aligned = next_payday(target, customer.payday_weekdays(rules))
                    if aligned != target:
                        source = (
                            f"observed payday ({_weekday_name(customer.payday_weekday)})"
                            if customer.has_observed_payday
                            else "default payday window (Thu/Fri)"
                        )
                        note_parts.append(f"Aligned to customer's {source}.")
                    target = aligned

            # Applied to every retry, aligned or not: a fixed +24h technical
            # retry lands on a Saturday one week in seven, and the bank would
            # hold it until Monday anyway.
            target, moved_off = business_day(target)
            if moved_off is not None:
                note_parts.append(
                    f"Rolled forward off {moved_off} — banks do not process then."
                )

            if action.silent:
                note_parts.append("Silent — customer is not told.")
            if repeats > 1:
                note_parts.append(f"Retry {number} of {repeats}.")
            previous = target

            if target > write_off_at:
                add(
                    ActionType.RETRY,
                    None,
                    f"Skipped: retry {number} of {repeats} would land "
                    f"{target.date().isoformat()}, past the "
                    f"{write_off_days}-day write-off horizon.",
                    status=AttemptStatus.SKIPPED,
                )
                continue

            add(ActionType.RETRY, target, " ".join(note_parts) or "Scheduled retry.")

        if repeats:
            when = "on the next payday" if action.align_to_payday else "on a fixed interval"
            trace.append(
                f"Scheduled {repeats} retr{'y' if repeats == 1 else 'ies'} "
                f"{when}, first at +{action.delay_hours}h."
            )
        if monthly_escalates:
            trace.append(
                f"{_money(payment.amount_cents)} reads as a monthly bill, so "
                f"attempt {rules.monthly_escalation_attempt} steps to next "
                "month's equivalent date rather than a third weekly payday, and "
                f"the write-off horizon extends to {write_off_days} days to "
                "cover it."
            )

    # --- customer messages, subject to the frequency cap --------------------
    # Walked in schedule order so the cap applies to the sequence the customer
    # actually experiences, not the order the YAML happens to list.
    message_actions = sorted(
        (a for a in strategy.actions if a.action in CUSTOMER_FACING_ACTIONS),
        key=lambda a: a.delay_hours,
    )
    last_contact = customer.last_message_at
    min_gap = rules.min_hours_between_customer_messages

    # An offer to split the balance is a response to repeated failure, so it is
    # anchored behind the last retry rather than to a fixed offset from the
    # dishonour. Sending it on day 10 while a retry is still due on day 12 would
    # tell the customer we have given up on an attempt we are about to make.
    last_retry_at = max(
        (
            a.scheduled_for
            for a in attempts
            if a.action is ActionType.RETRY and a.scheduled_for is not None
        ),
        default=None,
    )

    for action in message_actions:
        target = hours_from(at, action.delay_hours)
        note_parts = []

        if action.action is ActionType.OFFER_SPLIT:
            if last_retry_at is None:
                # No retries were scheduled, so there is no "repeated failure"
                # for the offer to follow. Suppressed rather than sent early.
                add(
                    action.action,
                    None,
                    "Skipped: no retries were scheduled, so there is no "
                    "repeated failure for a split offer to follow.",
                    channel=action.channel,
                    status=AttemptStatus.SKIPPED,
                )
                continue
            settled_by = hours_from(last_retry_at, 24)
            if settled_by > target:
                target = settled_by
                note_parts.append(
                    "Held until the scheduled retries have run — the offer only "
                    "makes sense once the full amount has failed twice."
                )

        if last_contact is not None:
            earliest = hours_from(last_contact, min_gap)
            if target < earliest:
                # Delayed rather than dropped: the cap exists to stop us pestering
                # people, not to lose a message the customer needs to receive.
                note_parts.append(
                    f"Delayed to respect the {min_gap}h minimum gap between "
                    "customer messages."
                )
                target = earliest

        if target > write_off_at:
            add(
                action.action,
                None,
                f"Skipped: falls beyond the {rules.write_off_after_days}-day "
                "write-off horizon.",
                channel=action.channel,
                status=AttemptStatus.SKIPPED,
            )
            continue

        if action.tone:
            note_parts.append(f"Tone: {action.tone}.")
        channel_label = action.channel.value if action.channel else "email"
        add(
            action.action,
            target,
            " ".join(note_parts) or f"Sent by {channel_label}.",
            channel=action.channel,
        )
        last_contact = target

    if message_actions:
        channels = ", ".join(
            sorted({a.channel.value for a in message_actions if a.channel})
        )
        trace.append(
            f"Customer contact scheduled via {channels or 'email'}, "
            f"never closer together than {min_gap}h."
        )
    if any(
        a.action is ActionType.OFFER_SPLIT and a.status is AttemptStatus.SCHEDULED
        for a in attempts
    ):
        halves = split_halves(payment.amount_cents)
        trace.append(
            f"If the retries both come back short, the customer is offered "
            f"{_money(payment.amount_cents)} as "
            f"{_money(halves[0])} + {_money(halves[1])} across two paydays "
            "rather than the same figure a third time."
        )

    # --- human escalation ---------------------------------------------------
    for action in (a for a in strategy.actions if a.action is ActionType.NOTIFY_HUMAN):
        target = hours_from(at, action.delay_hours)
        add(
            ActionType.NOTIFY_HUMAN,
            target,
            "Account owner alerted."
            if action.delay_hours == 0
            else f"Account owner alerted if unresolved after {action.delay_hours}h.",
            channel=action.channel,
        )
    if strategy.notify_human:
        trace.append("A human is told about this one regardless of outcome.")

    # --- write-off ----------------------------------------------------------
    add(
        ActionType.WRITE_OFF,
        write_off_at,
        f"Written off if still unrecovered {write_off_days} days after the "
        "failure.",
    )
    trace.append(
        f"Write-off horizon {write_off_days} days after the failure "
        f"({write_off_at.date().isoformat()})."
    )

    attempts.sort(
        key=lambda a: (
            a.scheduled_for is None,  # skipped (no time) sort last
            a.scheduled_for or write_off_at,
            a.attempt_number,
        )
    )
    # Renumber after sorting so attempt_number reads 1..n down the rendered
    # timeline. It is a display ordinal, not a stable key — ids are the key.
    for i, attempt in enumerate(attempts, start=1):
        attempt.attempt_number = i

    from app.services.reasoning import build_reasoning

    reasoning = build_reasoning(
        payment=payment,
        failure_class=failure_class,
        strategy=strategy,
        attempts=attempts,
        customer=customer,
        rules=rules,
        write_off_days=write_off_days,
    )

    return RecoveryPlan(
        payment_id=payment.id,
        failure_class=failure_class,
        strategy=strategy,
        attempts=attempts,
        reasoning=reasoning,
        notify_human=strategy.notify_human,
        write_off_at=write_off_at,
        decision_trace=trace,
    )


_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _weekday_name(weekday: int | None) -> str:
    if weekday is None:
        return "payday"
    return _WEEKDAYS[weekday % 7]


def apply_plan(payment: Payment, recovery: RecoveryPlan) -> Payment:
    """Attach a plan's output to a payment.

    Returns a copy rather than mutating in place so callers holding the old
    object do not see a half-updated payment. `status` is only touched to move a
    classified failure out of `pending`; recovered and written_off are terminal
    and set by the recovery service, not here.
    """
    updated = payment.model_copy(
        update={
            "failure_class": recovery.failure_class,
            "attempts": recovery.attempts,
            "reasoning": recovery.reasoning,
        }
    )
    if updated.status is PaymentStatus.PENDING:
        updated = updated.model_copy(update={"status": PaymentStatus.FAILED})
    return updated
