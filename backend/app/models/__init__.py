"""Model package.

Importing this module imports every mapped class, which is what populates
Base.metadata. Alembic's env.py relies on that: a model not reachable from
here is invisible to autogenerate and silently never gets a migration.
"""

from __future__ import annotations

from app.models.attempt import Attempt
from app.models.base import Base
from app.models.customer import Customer
from app.models.enums import (
    ActionType,
    AttemptStatus,
    Channel,
    FailureClass,
    PaymentStatus,
)
from app.models.outbox import OutboxMessage
from app.models.payment import Payment
from app.models.sim_webhook import SimulatedWebhook
from app.models.webhook_event import WebhookEvent

__all__ = [
    "ActionType",
    "Attempt",
    "AttemptStatus",
    "Base",
    "Channel",
    "Customer",
    "FailureClass",
    "OutboxMessage",
    "Payment",
    "PaymentStatus",
    "SimulatedWebhook",
    "WebhookEvent",
]
