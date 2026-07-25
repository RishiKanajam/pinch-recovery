# Interface Contract

**This file is the source of truth. Change it only by agreement — a unilateral edit breaks the other person's work.**

Both halves of the system are built against these shapes. Neither developer needs the
other's code to be running in order to make progress.

---

## Domain vocabulary

| Term | Meaning |
|---|---|
| **Payment** | A single attempted debit against a customer. Has a lifecycle. |
| **Dishonour** | A failed direct debit, carrying a reason code from the bank. |
| **Strategy** | The decision of what to do about a dishonour, derived from its code. |
| **Attempt** | One execution of a strategy (a retry, a message, an escalation). |
| **Recovery** | A payment that failed and later succeeded. |

---

## Money

Money is **integer cents**, everywhere. Never floats. Never `Decimal` over the wire.
Field name is always `amount_cents`. Currency is always `"AUD"` for the hackathon.

```json
{ "amount_cents": 4999, "currency": "AUD" }
```

---

## Enums

```
PaymentStatus:  pending | succeeded | failed | recovered | written_off
FailureClass:   insufficient_funds | invalid_account | authority_cancelled
                | payment_stopped | technical | expired_card | do_not_honour | unknown
ActionType:     retry | request_details_update | notify_human | save_offer | write_off | none
Channel:        email | sms | in_app | phone
AttemptStatus:  scheduled | executed | succeeded | failed | skipped
```

`FailureClass` is **ours**, not Pinch's. Pinch's raw code string is preserved separately
in `raw_code`. This decoupling means a wrong guess about Pinch's code strings costs one
mapping-table row, not a refactor.

---

## Core objects

### Payment

```json
{
  "id": "pay_01HX...",
  "customer_id": "cus_01HX...",
  "customer_name": "Marina Auto Detailing",
  "amount_cents": 24900,
  "currency": "AUD",
  "status": "failed",
  "raw_code": "AC01",
  "failure_class": "invalid_account",
  "failed_at": "2026-07-25T04:12:00Z",
  "recovered_at": null,
  "attempts": [ /* Attempt objects */ ],
  "reasoning": "Account not found at the receiving institution. Retrying would incur a fee and cannot succeed. Routed straight to details update."
}
```

`reasoning` is a human-readable sentence explaining the decision. **It is required on every
payment that has been classified.** This field is the demo — the judge sees it, not the code.

### Attempt

```json
{
  "id": "att_01HX...",
  "payment_id": "pay_01HX...",
  "action": "retry",
  "channel": null,
  "status": "scheduled",
  "scheduled_for": "2026-07-30T22:00:00Z",
  "executed_at": null,
  "attempt_number": 2,
  "note": "Scheduled for customer's observed payday (Thursday)."
}
```

### Strategy (returned by the engine, not persisted directly)

```json
{
  "failure_class": "insufficient_funds",
  "actions": [
    { "action": "retry", "delay_hours": 120, "align_to_payday": true },
    { "action": "request_details_update", "channel": "email", "delay_hours": 0 }
  ],
  "max_attempts": 4,
  "notify_human": false,
  "reasoning": "Timing problem, not intent. Customer retains the service."
}
```

---

## HTTP API

Base: `/api/v1`. All responses JSON. Errors use `{ "error": { "code", "message" } }`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/payments` | List. Query: `status`, `failure_class`, `limit`, `cursor`. |
| `GET` | `/payments/{id}` | Single payment with attempts and reasoning. |
| `GET` | `/dashboard/summary` | Aggregates for the top of the dashboard. |
| `POST` | `/payments/{id}/run-recovery` | Force the engine to classify + schedule. Idempotent. |
| `GET` | `/customers/{id}/payment-method` | For the update-details page. |
| `POST` | `/customers/{id}/payment-method` | Submit new details, triggers immediate retry. |
| `GET` | `/outbox` | The fake inbox — every message the system "sent". |
| `POST` | `/webhooks/pinch` | Ingest. Must be idempotent on `event_id`. |
| `POST` | `/sim/scenarios` | Create a simulated failure. |
| `POST` | `/sim/fast-forward` | Advance simulated clock. Body: `{ "seconds": 259200 }`. |
| `POST` | `/sim/reset` | Wipe to a known seeded state. Used before every demo run. |

### `GET /dashboard/summary`

```json
{
  "at_risk_cents": 1840000,
  "recovered_cents": 1120000,
  "escalated_cents": 410000,
  "written_off_cents": 310000,
  "recovery_rate": 0.61,
  "by_class": [
    { "failure_class": "insufficient_funds", "count": 22, "amount_cents": 780000, "recovered_cents": 640000 }
  ]
}
```

### `POST /sim/scenarios`

```json
{
  "customer_id": "cus_01HX...",
  "amount_cents": 24900,
  "outcome": "dishonour",
  "raw_code": "AC01",
  "delay_seconds": 259200,
  "webhook_deliveries": 1
}
```

`webhook_deliveries: 2` fires the identical event twice. The ledger must absorb this
without double-counting. There is a test for it.

---

## The simulated clock

Everything time-based reads from `app.core.clock.now()`, never `datetime.utcnow()`.
`POST /sim/fast-forward` moves an offset. This is what makes a 3-day settlement window
demoable in 3 seconds, and it is non-negotiable — a direct `utcnow()` call anywhere in
business logic breaks the demo.

There is a lint test that greps for `utcnow` outside `clock.py` and fails the build.

---

## Mock mode

`PINCH_MODE=mock` (default) routes all outbound Pinch calls to the simulator.
`PINCH_MODE=live` hits the real sandbox. Frontend never knows the difference.
Build everything in mock. Switch to live once, late, deliberately.
