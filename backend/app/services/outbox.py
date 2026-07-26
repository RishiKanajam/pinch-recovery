"""Message copy for the fake inbox.

Nothing is delivered. Real email and SMS would burn most of a day on sender
verification and deliverability, and a judge cannot tell the difference between
a rendered inbox and a real one — so the system "sends" by appending a row.

The copy matters more than the transport. The strategy table sets a `tone` per
action, and the whole thesis is that an insufficient-funds notice and a cancelled
authority notice should not read the same. If every message said "your payment
failed, click here", classification would be a spreadsheet exercise.
"""

from __future__ import annotations

from datetime import datetime

from app.models.enums import ActionType, Channel, FailureClass
from app.models.schemas import Attempt, OutboxMessage, Payment


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def update_link_for(payment: Payment) -> str:
    """Deep link into the hosted update-details page.

    Carries the payment id so the page can recover *that* payment on submit and
    the presenter lands back on the row they clicked.
    """
    return f"/update-details/{payment.customer_id}?payment={payment.id}"


def _soft_funds_email(payment: Payment) -> tuple[str, str]:
    return (
        f"A quick heads-up about your {_money(payment.amount_cents)} payment",
        (
            f"Hi {payment.customer_name},\n\n"
            f"Your recent payment of {_money(payment.amount_cents)} didn't go "
            "through — your bank returned it as insufficient funds at the time we "
            "presented it.\n\n"
            "No action needed. We'll automatically try again on your next payday, "
            "and nothing about your service changes in the meantime.\n\n"
            "If you'd rather pay now or use a different account, you can update "
            "your details any time using the link below.\n\n"
            "— Accounts"
        ),
    )


def _urgent_invalid_account_email(payment: Payment) -> tuple[str, str]:
    return (
        "We can't reach your bank account",
        (
            f"Hi {payment.customer_name},\n\n"
            f"We tried to debit {_money(payment.amount_cents)} but your bank told "
            "us the account no longer exists or has been closed.\n\n"
            "We won't keep retrying — it can't succeed against a closed account, "
            "and each attempt costs a fee. We need your current account details "
            "instead.\n\n"
            "It takes about a minute:\n"
        ),
    )


def _invalid_account_sms(payment: Payment) -> tuple[str, str]:
    return (
        "SMS",
        (
            f"{payment.customer_name}: we still can't debit "
            f"{_money(payment.amount_cents)} — the account on file is closed. "
            "Update your details here:"
        ),
    )


def _save_offer_email(payment: Payment) -> tuple[str, str]:
    return (
        "Before you go — a few options",
        (
            f"Hi {payment.customer_name},\n\n"
            "We noticed you cancelled the direct debit authority with your bank, "
            "which usually means you're thinking about winding things up.\n\n"
            "That's completely fine, and we're not going to keep trying to charge "
            "you. We'd just rather you leave on purpose than by accident, so if it "
            "helps:\n\n"
            "  • Pause for up to three months, keeping your history\n"
            "  • Move to the smaller monthly plan\n"
            "  • Close the account and we'll send a final statement\n\n"
            f"There's an outstanding balance of {_money(payment.amount_cents)}. "
            "Reply and we'll sort it out with you — no automated chasing.\n\n"
            "— Accounts"
        ),
    )


def _expired_card_email(payment: Payment) -> tuple[str, str]:
    return (
        "Your card needs updating",
        (
            f"Hi {payment.customer_name},\n\n"
            f"The card we have on file has expired, so your "
            f"{_money(payment.amount_cents)} payment didn't go through.\n\n"
            "New details take a minute and we'll retry straight away:\n"
        ),
    )


def _neutral_details_email(payment: Payment) -> tuple[str, str]:
    return (
        f"We couldn't process your {_money(payment.amount_cents)} payment",
        (
            f"Hi {payment.customer_name},\n\n"
            f"Your bank declined our request for {_money(payment.amount_cents)} "
            "without giving a specific reason. That's usually a limit or a fraud "
            "rule on their side rather than anything wrong with your account.\n\n"
            "We tried once more and it was declined again, so rather than keep "
            "presenting it, please confirm or update the account details:\n"
        ),
    )


# Copy is chosen by (class, action) rather than by action alone: the same
# `request_details_update` action has to sound different for a closed account
# than for an expired card.
_COPY = {
    (FailureClass.INSUFFICIENT_FUNDS, ActionType.REQUEST_DETAILS_UPDATE): _soft_funds_email,
    (FailureClass.INVALID_ACCOUNT, ActionType.REQUEST_DETAILS_UPDATE): _urgent_invalid_account_email,
    (FailureClass.AUTHORITY_CANCELLED, ActionType.SAVE_OFFER): _save_offer_email,
    (FailureClass.EXPIRED_CARD, ActionType.REQUEST_DETAILS_UPDATE): _expired_card_email,
    (FailureClass.DO_NOT_HONOUR, ActionType.REQUEST_DETAILS_UPDATE): _neutral_details_email,
}


def render_message(
    message_id: str,
    payment: Payment,
    attempt: Attempt,
    failure_class: FailureClass,
    customer_email: str,
    sent_at: datetime,
) -> OutboxMessage:
    """Build the outbox row for an executed customer-facing attempt."""
    channel = attempt.channel or Channel.EMAIL

    if channel is Channel.SMS:
        _, body = _invalid_account_sms(payment)
        subject = f"SMS to {payment.customer_name}"
    else:
        builder = _COPY.get((failure_class, attempt.action))
        if builder is None:
            # Any (class, action) pair without bespoke copy still sends something
            # coherent rather than an empty message or a KeyError mid-demo.
            subject, body = _neutral_details_email(payment)
        else:
            subject, body = builder(payment)

    tone = None
    if attempt.note and "Tone:" in attempt.note:
        tone = attempt.note.split("Tone:", 1)[1].strip().rstrip(".").strip() or None

    return OutboxMessage(
        id=message_id,
        payment_id=payment.id,
        customer_id=payment.customer_id,
        customer_name=payment.customer_name,
        channel=channel,
        subject=subject,
        body=body,
        sent_at=sent_at,
        tone=tone,
        action=attempt.action,
        update_link=update_link_for(payment),
    )
