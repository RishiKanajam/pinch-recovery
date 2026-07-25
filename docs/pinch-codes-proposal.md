# Proposal: correct `raw_codes` in strategies.yaml

**From:** Person A (ingestion) · **For:** Person B (classifier) · **Date:** 2026-07-25
**Patch:** `docs/strategies-raw-codes.patch` — apply with `git apply docs/strategies-raw-codes.patch`

`strategies.yaml` is your file, so nothing here is applied. The patch is proposed,
verified to apply cleanly against the current file, and reversible.

---

## The problem

Every `raw_code` currently in `strategies.yaml` is invented. Verified against
[Pinch's dishonour codes](https://docs.getpinch.com.au/docs/dishonour-codes) today:
`AM04`, `AC01`, `AC04`, `MD01`, `MS02`, `AG01`, `05`, `54`, `0902`–`0909` — none of
them exist. They are ISO 20022 / card-scheme vocabulary; Pinch uses none of it.

Left as-is, every real dishonour classifies as `unknown` and the demo shows one
strategy instead of seven.

## What Pinch actually emits

Seven codes, all hyphenated lowercase, delivered on `bank-results` webhook events.
In test mode each is triggered by putting `#code` in the payment description or the
payer's first name.

| Pinch code | Meaning | Pinch says retryable |
|---|---|---|
| `insufficient-funds` | Not enough funds at time of transaction | Yes |
| `temporary-problem` | Unusual, too frequent, or otherwise irregular | Yes |
| `technical-error` | Failure on Pinch's side | Yes |
| `blocked-by-bank` | Suspicious, fraud, frozen, or too risky | No |
| `invalid-account` | Bank account details invalid | No |
| `invalid-card` | Card details invalid | No |
| `unsupported-card` | Card type not supported | No |

## Proposed mapping

| Our `FailureClass` | Proposed `raw_codes` | Note |
|---|---|---|
| `insufficient_funds` | `insufficient-funds` | Exact match |
| `invalid_account` | `invalid-account` | Exact match |
| `technical` | `technical-error`, `temporary-problem` | Both are bank/processor-side and both retryable |
| `do_not_honour` | `blocked-by-bank` | Closest fit — see open question 1 |
| `expired_card` | `invalid-card`, `unsupported-card` | Card-only; no direct debit equivalent |
| `authority_cancelled` | `authority-cancelled` | **Simulator-only — no Pinch code exists** |
| `payment_stopped` | `payment-stopped` | **Simulator-only — no Pinch code exists** |
| `unknown` | *(empty)* | Unchanged; catch-all |

The two synthetic codes keep Pinch's hyphenated style so the classifier needs no
special-casing, and are marked `SIMULATOR-ONLY` inline in the patch.

## The part worth your attention

**`authority_cancelled` and `payment_stopped` have no Pinch dishonour code.**

Those two carry the product's argument — cancelled authority is a churn signal, not a
retry; a stopped payment is a dispute, humans only. They're what separates this from a
cron job, and Pinch cannot produce them from a real dishonour.

This does not block the demo: the simulator generates webhooks in mock mode, so
`/sim/seed-demo` still produces all seven classes and the full strategy table is
visible. But if we ever run the demo against live-test, only five classes can occur,
and a judge who knows the API could ask.

Three options:

1. **Keep seven classes, mark two simulator-only.** What the patch does. The strategy
   table stays intact and the provenance is honest.
2. **Collapse to five.** Safest under scrutiny, but loses the two strategies that make
   the pitch.
3. **Leave the codes wrong.** Only survivable if we never touch live.

I'd take 1, but it's your call — you own the classifier and the reasoning strings.

## Open questions for you

1. **`blocked-by-bank` is non-retryable per Pinch, but `do_not_honour` retries once**
   (`max_attempts: 1`, `delay_hours: 48`). Retrying a bank block risks the merchant
   being flagged — which your own `reasoning` string already says. Should
   `do_not_honour` drop to `max_attempts: 0`? The patch leaves your values alone and
   flags the contradiction inline.
2. **`expired_card` is card-only.** Direct debit has no expiry, so on a DD-only demo
   this class can never fire. Keep it for card-funded subscriptions, or cut it?
3. **`temporary-problem` folded into `technical`.** Reasonable — both are
   not-the-customer's-fault and retryable — but it is arguably its own class, since
   "too close together" is a rate problem a longer delay fixes rather than a silent
   retry.

## What I need back

Just which option you want on the two orphan classes. My seed data emits `raw_code`
values straight from this table, so whatever lands here is what
`/sim/seed-demo` produces. If the table changes after I seed, the demo data is wrong.
