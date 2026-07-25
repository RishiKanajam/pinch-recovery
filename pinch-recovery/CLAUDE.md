# CLAUDE.md — Pinch Recovery Engine (Person A: Ingestion & Simulation)

Claude Code reads this file automatically at the start of every session. It is the
standing context for my slice of the project. Read `docs/CONTRACT.md` before writing
any code — it is the source of truth for API shapes, enums, and field names, and it is
shared with my teammate. Do not change `docs/CONTRACT.md` without me saying so explicitly.

## What this project is

A failed-payment recovery engine on the Pinch Payments API. Direct debit dishonours carry
a reason code; the product reads the code and applies a code-specific recovery strategy
instead of blind retries. Two-person hackathon team, submission due tomorrow.

## My slice (Person A): Ingestion & Simulation

I own everything that produces and records a failed payment:
- Postgres schema + Alembic migrations (`customers`, `payments`, `attempts`, `webhook_events`)
- `POST /webhooks/pinch` — ingest, idempotent on `event_id`
- The simulator: `/sim/scenarios`, `/sim/fast-forward`, `/sim/reset`, `/sim/seed-demo`
- Pinch API client with a mock/live switch
- A realistic seed dataset across all seven failure classes

My teammate (Person B) owns the classifier, strategy engine, dashboard, and the
update-details flow. I do NOT build those. Where our code meets, `docs/CONTRACT.md`
is the seam — I produce data in exactly the shapes it specifies so their code binds
without translation.

## Non-negotiable rules (a build check enforces each one)

1. **Money is integer cents.** DB columns are `BigInteger`. The field is always
   `amount_cents`. Never a float, never `Numeric`, never `Decimal` over the wire.
2. **Time comes from `app.core.clock.now()`.** Never `datetime.utcnow()` or
   `datetime.now()` in business logic. `tests/test_clock_discipline.py` greps for
   violations and fails the build. The simulator's delays and the fast-forward demo
   depend on this — a direct wall-clock call silently breaks fast-forward.
3. **Webhooks are idempotent on `event_id`.** Insert into `webhook_events` first; if the
   id already exists, return 200 and do nothing else. The same event twice = exactly one
   payment row. There is a test that delivers a duplicate and asserts this.
4. **Match `docs/CONTRACT.md` field names exactly.** `amount_cents`, `raw_code`,
   `failure_class`, `failed_at`, etc. A renamed field breaks my teammate silently.

## Stack

FastAPI + SQLAlchemy 2.0 + Alembic + Postgres (psycopg 3). Pydantic v2 for schemas.
APScheduler for due-action polling. pytest for tests. Dependencies are pinned in
`backend/requirements.txt` — use those versions, don't upgrade them mid-hackathon.

## How I want you to work

- Work in small steps. After each file or endpoint, run the tests and show me the result
  before moving on. A green check between steps beats a big diff I can't review.
- Write the test alongside the code, not after. Especially the idempotency test and any
  clock-dependent logic — freeze the clock in tests for deterministic assertions.
- When something is ambiguous, check `docs/CONTRACT.md` first. If it's still ambiguous,
  ask me rather than guessing at a field name or shape.
- Keep the mock path first-class. `PINCH_MODE=mock` is the default and everything must
  work end-to-end without real Pinch credentials.
- Explain what you changed in two or three sentences, then stop. I'm moving fast and
  reviewing continuously; I don't need long write-ups.

## Definition of done for my slice

A single curl to `/sim/seed-demo` produces ~50 realistic failed payments across all seven
classes; `/sim/fast-forward` collapses a three-day settlement window into seconds; a
duplicate webhook leaves one ledger row; and my teammate can read all of it through the
endpoints in `docs/CONTRACT.md` without asking me what a field is called.
