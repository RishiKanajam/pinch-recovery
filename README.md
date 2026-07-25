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
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d db          # postgres on 5432
alembic upgrade head
uvicorn app.main:app --reload    # :8000

# frontend
cd frontend
npm install
npm run dev                      # :3000
```

Default mode is `PINCH_MODE=mock` — no Pinch credentials needed to develop.

```bash
# seed a demo dataset and run the whole recovery cycle in ~10 seconds
curl -X POST localhost:8000/api/v1/sim/reset
curl -X POST localhost:8000/api/v1/sim/seed-demo
curl -X POST localhost:8000/api/v1/sim/fast-forward -d '{"seconds": 259200}'
```

---

## Architecture

```
Pinch webhook ──► /webhooks/pinch ──► ledger (idempotent on event_id)
                                          │
                                          ▼
                                   classifier  ── strategies.yaml
                                          │        (failure class + reasoning)
                                          ▼
                                    scheduler ── clock.now()
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    ▼                     ▼                     ▼
                 retry              outbox message        human escalation
              (via Pinch)          (fake inbox in UI)      (dashboard flag)
```

The simulator sits in front of the Pinch client in mock mode and produces dishonours
on demand, with a compressible settlement delay.

### Key documents

| File | What it is |
|---|---|
| `docs/CONTRACT.md` | **Read first.** API shapes, enums, money rules. The interface both halves build against. |
| `backend/app/services/strategies.yaml` | The recovery strategy table. This is the product. |
| `docs/DEMO.md` | The five-minute demo script, step by step. |

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
- Seed dataset — ~50 payments across all seven failure classes, realistic AU service
  business names and amounts
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
