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

### `GET /payments`

Returns a page of payments, newest failure first.

```json
{
  "data": [ /* Payment objects, as specified in Core objects above */ ],
  "next_cursor": "2026-07-22T04:12:00Z|pay_01HX..."
}
```

| Query param | Default | Notes |
|---|---|---|
| `status` | — | Exact match on `PaymentStatus`. |
| `failure_class` | — | Exact match on `FailureClass`. Matches nothing until the classifier has run. |
| `limit` | `50` | Max `200`. |
| `cursor` | — | Opaque. See below. |

**Pagination is keyset, not offset.** The simulator and webhook ingest insert rows
between requests, and an offset would silently skip or repeat rows as the set shifts
underneath a paging dashboard.

- `next_cursor` is **opaque** — echo it back as `?cursor=<value>` to get the next page.
  Do not parse it, construct it, or rely on its internal format.
- `next_cursor` is `null` on the last page. That, not an empty `data` array, is the
  signal to stop.
- A cursor this endpoint did not issue returns `400` with
  `{ "error": { "code": "invalid_cursor", ... } }` rather than silently returning the
  first page again, which is indistinguishable from a pagination loop.

**Ordering** is two regions: payments that have failed, `failed_at` descending, then
payments with no `failed_at` at all (a `pending` payment). `id` descending breaks ties.
Undated rows sort **last** — Postgres sorts `NULL` first under `DESC`, which would put
unfailed payments above real failures.

**Fields owned by the engine are present from the first response, not added later.**
On a payment that has not yet been classified, `failure_class` and `reasoning` are
`null` and `attempts` is `[]`. They are never absent from the object.

### `GET /payments/{id}`

Returns a **bare Payment object** — not wrapped in `data` — matching the Core objects
shape above, with `attempts` populated.

Unknown id returns `404` in the standard error shape:

```json
{ "error": { "code": "payment_not_found", "message": "No payment pay_01HX..." } }
```

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

---

## Ingestion-internal (Person A owns, Person B does not call)

The shapes below sit entirely on the ingestion side of the seam. Person B reads
`payments` rows through the API above and never touches these directly — they are
documented here so the contract matches the code, not because they cross the seam.
Added by agreement with Person B.

### Inbound webhook envelope — `POST /webhooks/pinch`

The event Pinch delivers when a payment's state changes. Modelled on Pinch's real
webhook shape — **verify against the live payload before switching `PINCH_MODE=live`**;
the mock simulator emits this exact envelope so both modes ingest identically.

```json
{
  "event_id": "evt_01HX...",
  "event_type": "payment.dishonoured",
  "created_at": "2026-07-25T04:12:00Z",
  "data": {
    "payment_id": "pay_01HX...",
    "customer_id": "cus_01HX...",
    "amount_cents": 24900,
    "currency": "AUD",
    "dishonour_code": "AC01"
  }
}
```

- `event_id` is the idempotency key. Ingest inserts it into `webhook_events` first; a
  duplicate `event_id` returns 200 and is otherwise a no-op. Same event twice = one
  payment row.
- `event_type` values used for the hackathon: `payment.dishonoured` (the one that
  matters), `payment.succeeded` (marks a prior failure recovered).
- `dishonour_code` maps to `raw_code` on the payment, then to a `FailureClass` by the
  classifier. It must be one of the strings in `strategies.yaml` `raw_codes`, or it
  classifies as `unknown`.
- `# TODO verify against live webhook` — the field names inside `data` are the most
  likely thing to differ from Pinch's real shape. This is the known checkpoint for the
  mock→live switch.

### Dev-only simulator endpoints

Not called by the frontend or by Person B's code. Local development and demo control only.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sim/seed-demo` | Reset, then create ~50 realistic failed payments across all seven failure classes. Run before a demo. Returns a per-class count + `amount_cents` summary. |

`/sim/scenarios`, `/sim/fast-forward`, and `/sim/reset` are already specified in the
main HTTP table above; `seed-demo` is grouped here because, unlike those, it is purely a
demo-seeding convenience with no counterpart in a live deployment.

**`POST /sim/seed-demo` response:**

```json
{
  "seeded": 50,
  "at_risk_cents": 1840000,
  "by_class": [
    { "failure_class": "insufficient_funds", "count": 20, "amount_cents": 760000 },
    { "failure_class": "invalid_account", "count": 9, "amount_cents": 410000 }
  ]
}
```