"""The `reasoning` string: one human paragraph per classified payment.

README non-negotiable 4 — the judge reads this field, not the code. A payment
without reasoning is an unfinished feature, so `build_reasoning` is written to
be incapable of returning an empty string: every branch has a fallback, and
there is a test that walks all eight classes and asserts real prose comes out.

The voice is deliberately flat and specific. "Account not found at the receiving
institution" beats "AC01 detected", and naming the actual money and the actual
weekday beats a template with the nouns swapped out. It should read like a
competent ops person explaining the decision, because that is the claim the
product is making.
"""

from __future__ import annotations

from datetime import datetime

from app.models.enums import ActionType, AttemptStatus, FailureClass
from app.models.schemas import Attempt, Payment, Strategy
from app.services.classifier import GlobalRules

# What actually happened at the bank, in a sentence a merchant would recognise.
# Keyed by class rather than by raw code so a new code string inherits the right
# explanation for free.
_WHAT_HAPPENED: dict[FailureClass, str] = {
    FailureClass.INSUFFICIENT_FUNDS: (
        "The account was valid but did not hold enough money when the debit was "
        "presented"
    ),
    FailureClass.INVALID_ACCOUNT: (
        "The receiving account does not exist or has been closed"
    ),
    FailureClass.AUTHORITY_CANCELLED: (
        "The customer cancelled the direct debit authority at their bank"
    ),
    FailureClass.PAYMENT_STOPPED: (
        "The customer placed a stop on this specific payment"
    ),
    FailureClass.TECHNICAL: (
        "The bank's processing failed on its own side — nothing to do with the "
        "customer or their balance"
    ),
    FailureClass.EXPIRED_CARD: ("The card on file has passed its expiry date"),
    FailureClass.DO_NOT_HONOUR: (
        "The issuer declined the debit without saying why — a risk rule, a limit, "
        "or something it will not disclose"
    ),
    FailureClass.UNKNOWN: (
        "The bank returned a dishonour code this system does not recognise yet"
    ),
}

# Why the chosen shape of response follows from that. This is the sentence that
# distinguishes the product from a retry cron, so it carries the argument.
_WHY_THIS_RESPONSE: dict[FailureClass, str] = {
    FailureClass.INSUFFICIENT_FUNDS: (
        "That is a timing problem, not a statement of intent — the customer still "
        "wants the service, so the money is more likely to be there on their next "
        "payday than on a fixed three-day timer"
    ),
    FailureClass.INVALID_ACCOUNT: (
        "No number of retries can change that, and each one costs a dishonour fee, "
        "so this goes straight to capturing correct details"
    ),
    FailureClass.AUTHORITY_CANCELLED: (
        "Cancelling the authority is a deliberate act, so retrying would be both "
        "futile and hostile — this is a churn conversation, not a billing one"
    ),
    FailureClass.PAYMENT_STOPPED: (
        "A stop order is a dispute signal, and any automated chase risks turning it "
        "into a formal complaint or chargeback, so a human owns it from here"
    ),
    FailureClass.TECHNICAL: (
        "The customer is unaware anything happened, and telling them would "
        "manufacture churn out of a bank outage, so the retry stays silent"
    ),
    FailureClass.EXPIRED_CARD: (
        "Expiry is knowable in advance, so recovery here is the fallback — one "
        "retry in case the card was already replaced, then ask for new details"
    ),
    FailureClass.DO_NOT_HONOUR: (
        "One retry is worth attempting because the decline may be transient, but "
        "repeated attempts risk the merchant being flagged, so the cap is hard"
    ),
    FailureClass.UNKNOWN: (
        "Guessing would be worse than asking, so this takes one cautious retry and "
        "a human is told so the mapping table can be extended"
    ),
}

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _weekday(dt: datetime) -> str:
    return _WEEKDAYS[dt.weekday()]


def _describe_plan(attempts: list[Attempt]) -> str:
    """One sentence describing what was actually scheduled."""
    scheduled = [a for a in attempts if a.status is AttemptStatus.SCHEDULED]
    retries = [a for a in scheduled if a.action is ActionType.RETRY]
    messages = [
        a
        for a in scheduled
        if a.action in (ActionType.REQUEST_DETAILS_UPDATE, ActionType.SAVE_OFFER)
    ]
    escalations = [a for a in scheduled if a.action is ActionType.NOTIFY_HUMAN]

    parts: list[str] = []

    if retries:
        first = retries[0]
        when = ""
        if first.scheduled_for is not None:
            when = f" starting {_weekday(first.scheduled_for)}"
        if len(retries) == 1:
            parts.append(f"one retry scheduled{when}")
        else:
            parts.append(f"{len(retries)} retries scheduled{when}")
    else:
        parts.append("no retries")

    if messages:
        channels: list[str] = []
        for message in messages:
            label = message.channel.value if message.channel else "email"
            if label not in channels:
                channels.append(label)
        action_label = (
            "a save offer"
            if any(m.action is ActionType.SAVE_OFFER for m in messages)
            else "a request for updated details"
        )
        parts.append(f"{action_label} by {' then '.join(channels)}")

    if escalations:
        parts.append("a human flagged on the dashboard")

    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _suppressions(attempts: list[Attempt], rules: GlobalRules) -> list[str]:
    """Sentences for rules that actively changed the plan.

    Only emitted when a rule fired. A paragraph explaining rules that did not
    apply reads like boilerplate and trains the reader to skip the field.
    """
    notes: list[str] = []
    skipped = [a for a in attempts if a.status is AttemptStatus.SKIPPED]

    if any("retry budget" in (a.note or "").lower() or "retries in the last" in (a.note or "").lower() for a in skipped):
        notes.append(
            f"Retries were suppressed because this customer has already used their "
            f"{rules.customer_max_retries_in_window}-retry budget for the last "
            f"{rules.customer_retry_budget_days} days — repeated failures across "
            "invoices are a relationship problem, not a billing one"
        )

    if any("write-off horizon" in (a.note or "").lower() for a in skipped):
        notes.append(
            f"Steps that would have landed past the {rules.write_off_after_days}-day "
            "write-off horizon were dropped rather than scheduled into a period "
            "where the payment is already closed"
        )

    if any("minimum gap" in (a.note or "").lower() for a in attempts):
        notes.append(
            f"Contact was pushed back to keep at least "
            f"{rules.min_hours_between_customer_messages} hours between messages to "
            "this customer across every channel"
        )

    if any("Aligned to customer's observed payday" in (a.note or "") for a in attempts):
        notes.append("The retry date comes from this customer's own payment history")

    return notes


def build_reasoning(
    payment: Payment,
    failure_class: FailureClass,
    strategy: Strategy,
    attempts: list[Attempt],
    customer=None,
    rules: GlobalRules | None = None,
) -> str:
    """A human paragraph explaining this payment's decision.

    Never returns an empty string. If every specific branch somehow misses, the
    class's own reasoning line from strategies.yaml is used, and that is
    validated as non-empty at load time.
    """
    rules = rules or GlobalRules()

    what = _WHAT_HAPPENED.get(failure_class, "")
    why = _WHY_THIS_RESPONSE.get(failure_class, "")

    sentences: list[str] = []

    code_label = f" (code {payment.raw_code})" if payment.raw_code else ""
    if what:
        sentences.append(
            f"{_money(payment.amount_cents)} from {payment.customer_name} failed: "
            f"{what}{code_label}."
        )
    if why:
        sentences.append(f"{why[0].upper()}{why[1:]}.")

    sentences.append(f"Plan: {_describe_plan(attempts)}.")

    for note in _suppressions(attempts, rules):
        sentences.append(f"{note}.")

    text = " ".join(s for s in sentences if s.strip())
    text = " ".join(text.split())

    if not text:
        # Unreachable in practice — the loader guarantees strategy.reasoning is
        # non-empty prose. Kept because a blank reasoning field is a product bug,
        # and a fallback is cheaper than a rendered empty cell.
        return strategy.reasoning or (
            f"Classified as {failure_class.value}; see the strategy table."
        )
    return text
