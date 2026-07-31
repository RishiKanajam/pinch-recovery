# Pinch Recovery Engine

Failed direct debits aren't all the same. An insufficient-funds dishonour is a timing
problem. An invalid-account dishonour is a data problem. A cancelled-authority dishonour
is a churn signal. Almost every business treats all three identically — retry in three
days, send "your payment failed", give up after the third try.

Pinch already tells you *why* the payment failed. This reads that reason and acts on it.

---

## Quick start

```bash
# backend
cd backend

# Python 3.12, not 3.14. The pins in requirements.txt have no 3.14 wheels —
# psycopg-binary==3.2.3 doesn't exist for it and pydantic-core builds from
# source. uv downloads 3.12 for you if you don't have it.
uv venv --python 3.12 .venv

# Activate. Do not skip this: `python3` on PATH is probably 3.14, and an
# unactivated alembic/uvicorn silently runs against the wrong interpreter.
source .venv/bin/activate

# `uv pip`, not bare `pip` — uv venvs ship without pip, so plain `pip` here
# resolves to the system 3.14 one and reinstalls into the wrong place.
uv pip install -r requirements.txt

docker compose up -d db          # postgres on 5432
alembic upgrade head
uvicorn app.main:app --reload    # :8000
```

There is no separate frontend build. The dashboard is server-rendered — FastAPI serves
Jinja2 templates from `app/web/templates` on the same `:8000` process as the API. Open
`http://localhost:8000/` once the server is up.

Default mode is `PINCH_MODE=mock` — no Pinch credentials needed to develop.

```bash
# seed a demo dataset and run the whole recovery cycle in ~10 seconds
curl -X POST localhost:8000/api/v1/sim/reset
curl -X POST localhost:8000/api/v1/sim/seed-demo
curl -X POST localhost:8000/api/v1/sim/fast-forward -d '{"seconds": 259200}'
```

### Running it against the real Pinch sandbox

Set `PINCH_MODE=live` with credentials in `backend/.env.local` (leave
`PINCH_API_BASE` on `/test/`) and the same loop runs against Pinch itself. The
sandbox dishonours a payment with whatever code you name, because the code goes
in the description prefixed with `#` — see
[`docs/pinch-sandbox-verification.md`](docs/pinch-sandbox-verification.md).

```bash
# a real payment in the Pinch test environment, forced to fail this way
curl -X POST localhost:8000/api/v1/pinch/test-payments \
  -H 'content-type: application/json' \
  -d '{"amount_cents": 24900, "raw_code": "insufficient-funds"}'

# jump past tonight's processing run — the Time-Travel header goes with it
curl -X POST "localhost:8000/ui/fast-forward?seconds=86400"

# pull the dishonour in, classify it, act on it (the background poller
# does this every 15s in live mode; this is the manual version)
curl -X POST localhost:8000/api/v1/pinch/poll \
  -H 'content-type: application/json' -d '{}'
```

Both endpoints work in mock mode too, driving the simulator instead — so the
demo sequence rehearsed on a laptop is the sequence run against the sandbox.

---

## Architecture

```
Pinch  ──► /webhooks/pinch ──┐
           (push, needs a    │
            public URL)      ├──► ledger (idempotent on event_id)
       ──► GET /events    ───┘         │
           (poll, always works)        ▼
                                   classifier  ── strategies.yaml
                                          │        (failure class + reasoning)
                                          ▼
                                    scheduler ── clock.now() + AU business days
                                          │
              ┌──────────────┬────────────┼────────────┬──────────────┐
              ▼              ▼            ▼            ▼              ▼
           retry       outbox message  split offer   human      write-off
        (via Pinch)   (fake inbox)    (instalments) escalation  (horizon)
```

Ingest accepts the same event from either route: both insert into
`webhook_events` first and let the unique index reject the duplicate, so
running webhooks and polling at once is safe rather than double-counted.

The simulator sits in front of the Pinch client in mock mode and produces
dishonours on demand, with a compressible settlement delay.

### Key documents

| File | What it is |
|---|---|
| `docs/CONTRACT.md` | **Read first.** API shapes, enums, money rules. The interface both halves build against. |
| `backend/app/services/strategies.yaml` | The recovery strategy table. This is the product. |
| `docs/pinch-sandbox-verification.md` | How the sandbox is made to produce a specific dishonour code, and how time travel is driven. |

---

## Non-negotiables

1. **Money is integer cents.** Never a float. Field is always `amount_cents`.
2. **Time comes from `app.core.clock.now()`.** A guard test fails the build otherwise.
3. **Webhooks are idempotent on `event_id`.** There is a test that delivers the same
   event twice and asserts one ledger row.
4. **Every classified payment carries a `reasoning` string.** The judge reads that field,
   not the code. A payment without reasoning is an incomplete feature.
5. **Hard failures are never retried.** Invalid account, cancelled authority, stopped
   payment — zero retries, always. This rule is the difference between this and a cron job.
6. **No retry is scheduled on a day the banks are shut.** Weekends and AU public
   holidays roll forward to the next business day, so the date on screen and the
   date the debit is presented are the same date. `app/core/holidays.py`.

---

## What the engine actually decides

| Failure | When it retries | What else happens |
|---|---|---|
| Insufficient funds | Next payday (Thu/Fri, or the customer's observed one), then the following payday. At a monthly-sized amount, attempt 3 steps to their next billing date instead of a third weekly try. | Soft notice on day one; an offer to split the balance in two if both payday attempts come back short. |
| Technical / temporary | Within 24h, silently. | Nothing — telling a customer about a bank outage manufactures churn. |
| Invalid account | Never. | Details capture by email, escalating to SMS at 48h and a human at 96h. |
| Authority cancelled | Never. | Save offer, and the account owner is told. This is churn, not billing. |
| Payment stopped | Never. | A human, immediately. Chasing a stop order invites a complaint. |
| Blocked by bank | Once, at 48h. | Then details capture. Pinch marks this non-retryable; we differ by exactly one attempt and say so in the reasoning. |

Across all of them: a retry budget per *customer* rather than per invoice, at
most one customer message per 24h on any channel, and a write-off horizon
(21 days, or 35 for a ladder that steps out to a monthly date) after which the
file closes rather than accruing dishonour fees.

---

## Team split

Two people, both strong on backend, so the cut is **vertical by domain** rather than
frontend/backend. Each person owns a full slice end to end. `docs/CONTRACT.md` is the
seam — agree changes to it, don't make them unilaterally.

### Person A — Ingestion & Simulation
**Owns:** everything that produces and records a failed payment.

- Postgres schema + Alembic migrations (`payments`, `attempts`, `customers`, `webhook_events`)
- `POST /webhooks/pinch` with idempotency on `event_id`
- The simulator: `/sim/scenarios`, `/sim/fast-forward`, `/sim/reset`, `/sim/seed-demo`
- Pinch API client with mock/live switch
- Seed dataset — ~50 payments across six of the seven failure classes (`expired_card`
  excluded — a card-scheme failure that can't arise from a direct debit), realistic AU
  service business names and amounts
- Tests: duplicate delivery, clock discipline, seed determinism

**Done when:** a single curl produces a realistic dishonour, and fast-forward makes a
three-day settlement window land in three seconds.

### Person B — Engine & Interface
**Owns:** everything that decides what to do and shows it to a human.

- Classifier: raw code → `FailureClass`, driven by `strategies.yaml`
- Strategy engine + scheduler, including payday alignment and the global rules
  (retry budget, message frequency cap, write-off horizon)
- The `reasoning` string generation — per payment, human-readable
- Dashboard: at-risk / recovered / escalated / written-off, breakdown by class,
  per-payment drill-down showing the decision trace
- Update-details page (hosted, mobile-first — a real customer would open this on a phone)
- Fake inbox rendering `/outbox`

**Done when:** clicking a failed payment shows its code, class, chosen strategy, and a
sentence explaining why — and the update-details flow recovers a payment live.

### Shared / pair on these
- `docs/CONTRACT.md` changes
- The demo script and its rehearsal
- Verifying real dishonour code strings against Pinch's docs, then updating
  `strategies.yaml` — do this together, once, around hour 20

### Sequencing

**Hours 0–4.** Both: read `CONTRACT.md`, agree it, then A does schema + migration while
B does classifier + strategy loader against hand-written fixtures. Neither blocks.

**Hours 4–12.** A: webhook ingest + simulator. B: scheduler + dashboard skeleton reading
from stub JSON matching the contract.

**Hour 12: first integration.** B points the dashboard at A's real API. Budget two hours
for this hurting. Everything after this point is against real data.

**Hours 12–24.** A: seed dataset + idempotency tests. B: update-details flow + reasoning
strings + drill-down view.

**Hours 24–36.** Both: the demo path only. Anything not on the demo path is now cut.
Rehearse it three times end to end. Fix what breaks in rehearsal, ignore what doesn't.

**Hours 36–48.** Freeze features. Write the submission. Record a backup video of the demo
working — live demos fail and a recording has saved more hackathon teams than any
last-minute feature.

### Rules for working in parallel
- Branch per slice, PR into `main`, no direct pushes.
- If you need a contract change, message first, edit second.
- Anything on the demo path beats anything off it. When in doubt, ask: does this appear
  in the five minutes the judge watches?
