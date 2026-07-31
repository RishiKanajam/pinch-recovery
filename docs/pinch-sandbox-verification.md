# Sandbox verification: can we force specific dishonour codes?

**Verified against the Pinch documentation on 2026-07-31.** This answers the one
question that gates the whole build: the pitch is that we branch on the failure
*reason*, so if the sandbox can only produce generic pass/fail, none of the
targeting or protocol work has realistic inputs and the demo cannot run live.

**Answer: yes.** Specific codes can be forced, on demand, from an ordinary API
call, and time can be moved forward to make settlement happen now. Everything
below is implemented in this repo, not just documented.

---

## 1. How a test payment forces a specific dishonour code

Put the code anywhere in the payment's `description`, or in the payer's
`firstName`, prefixed with `#`:

```json
POST https://api.getpinch.com.au/test/payments
{
  "payerId": "pyr_XXXX",
  "sourceId": "src_XXXX",
  "amount": 24900,
  "transactionDate": "2026-07-31",
  "description": "Recovery engine test payment #insufficient-funds"
}
```

The payment is submitted normally and comes back dishonoured with exactly that
code on the next `bank-results` event. Nothing else about the request is
special — which is the point: the code path the demo exercises is the code path
production uses.

Sources: [Test and Live Mode](https://docs.getpinch.com.au/docs/test-and-live-mode),
[Dishonour Codes](https://docs.getpinch.com.au/docs/dishonour-codes).

**In this repo:** `LivePinchClient.create_test_payment()` in
`backend/app/services/pinch_client.py`, exposed as
`POST /api/v1/pinch/test-payments`. It refuses outright when `PINCH_API_BASE`
points at `/live/` — there is no forced dishonour in production, only a real
debit against a real person.

```bash
curl -X POST localhost:8000/api/v1/pinch/test-payments \
  -H 'content-type: application/json' \
  -d '{"amount_cents": 24900, "raw_code": "insufficient-funds"}'
```

The same call in `PINCH_MODE=mock` drives the simulator instead, so the demo
sequence rehearsed on a laptop is the sequence run against the sandbox.

## 2. The exact code strings

Seven, all hyphenated lowercase. Pinch's own "can I try again?" column is worth
reading next to our classes, because two of them disagree with it deliberately.

| Pinch code | Pinch says retryable | Our class | What we do |
|---|---|---|---|
| `insufficient-funds` | Yes | `insufficient_funds` | Payday-timed retries, then an instalment offer |
| `temporary-problem` | Yes | `technical` | Silent retry within 24h |
| `technical-error` | Yes | `technical` | Silent retry within 24h |
| `blocked-by-bank` | No | `do_not_honour` | One retry, then details capture |
| `invalid-account` | No | `invalid_account` | **Zero retries.** Details capture, escalating channel |
| `invalid-card` | No | `expired_card` | Card-only; cannot occur on a direct debit |
| `unsupported-card` | No | `expired_card` | Card-only; cannot occur on a direct debit |

Two of our classes — `authority_cancelled` and `payment_stopped` — have **no
Pinch code** and are simulator-only. They carry a real part of the argument (a
cancelled mandate is churn, a stopped payment is a dispute), and a judge who
knows the API may ask. The honest answer is that Pinch surfaces mandate and
dispute state elsewhere, not as a dishonour code, and the strategies are
demonstrated on simulated events. See `docs/pinch-codes-proposal.md`.

`blocked-by-bank` is the one place we knowingly differ from Pinch's guidance:
they mark it non-retryable, we allow exactly one retry then stop. The reasoning
string says so on every such payment rather than hiding it.

## 3. How time travel is driven

A `Time-Travel` header on any test request, carrying the instant to pretend it
is:

```http
Time-Travel: 2026-08-01T09:45:59Z
```

Pinch then runs whatever processing, settlement, or scheduled-payment logic
would have run by that time. Direct debit dishonours arrive 1–3 business days
after presentation, so without this the demo would take a week.

**In this repo:** `LivePinchClient._request()` sends the header automatically,
derived from our own simulated clock (`app.core.clock`), and only when
`PINCH_API_BASE` is the test environment. That means one control — the `+3d`
button on the dashboard — advances our clock and Pinch's view of time together,
and the mock and live demos are driven identically.

## 4. Getting the failure back: webhook or poll

Both work, and both land in the same place. `POST /webhooks/pinch` needs a
public URL; on a laptop that means a tunnel, and a tunnel that dies mid-demo
looks exactly like the product not working. So the primary path is polling:

```bash
curl -X POST localhost:8000/api/v1/pinch/poll -H 'content-type: application/json' -d '{}'
```

`GET /events?eventType=bank-results` lists what happened, `GET /events/{id}`
fetches the payments in it, and each envelope goes through the *same* handler
the webhook uses — so an event delivered by both routes still produces exactly
one ledger row and one payment. In live mode the background poller does this
every `PINCH_POLL_SECONDS` (default 15) with no manual call at all.

One wrinkle worth knowing: webhooks arrive PascalCase (`Id`, `EventDate`) and
the Events API answers camelCase (`id`, `eventDate`). Ingest matches keys
case-insensitively (`PinchModel` in `backend/app/api/schemas.py`) rather than
picking one spelling, because a parser that understood only one would drop every
event from the other and look exactly like Pinch sending nothing.

## 5. The full live loop, end to end

```bash
# 0. credentials in backend/.env.local, PINCH_MODE=live,
#    PINCH_API_BASE=https://api.getpinch.com.au/test/   <-- test, not live

# 1. force a real dishonour in the sandbox
curl -X POST localhost:8000/api/v1/pinch/test-payments \
  -H 'content-type: application/json' \
  -d '{"amount_cents": 24900, "raw_code": "insufficient-funds", "customer_name": "Marina Auto Detailing"}'

# 2. jump past tonight's processing run (drives Pinch's clock too)
curl -X POST "localhost:8000/ui/fast-forward?seconds=86400"

# 3. pull the result in, classify it, and run whatever became due
curl -X POST localhost:8000/api/v1/pinch/poll -H 'content-type: application/json' -d '{}'
```

Step 3 is optional — the background poller does it — but running it by hand
makes the loop visible during a demo.

---

## What this changes about the plan

Nothing has to be simulated to demonstrate the core claim. Five of our seven
classes can be produced on demand from the real sandbox, with real events and
real retries. The two that cannot are simulator-only for a reason we can state
plainly, and mock mode remains the default so the whole system still runs with
no credentials at all.
