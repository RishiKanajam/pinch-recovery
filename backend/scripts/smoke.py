#!/usr/bin/env python
"""End-to-end check of Person A's slice against a running API.

This is the handoff gate. If it passes, Person B can build against these
endpoints without asking what a field is called: the ledger is readable in the
exact shape docs/CONTRACT.md specifies, duplicate webhooks do not double-count,
and a three-day settlement window collapses on demand.

    uvicorn app.main:app --port 8000
    python scripts/smoke.py --base-url http://127.0.0.1:8000

Exits non-zero on the first failure, so it can gate a demo rehearsal.
"""

from __future__ import annotations

import argparse
import sys

import httpx

# Every field docs/CONTRACT.md promises on a Payment. Person B's code binds to
# these names; a missing or renamed one breaks them silently.
CONTRACT_PAYMENT_FIELDS = {
    "id",
    "customer_id",
    "customer_name",
    "amount_cents",
    "currency",
    "status",
    "raw_code",
    "failure_class",
    "failed_at",
    "recovered_at",
    "attempts",
    "reasoning",
}

THREE_DAYS = 259_200

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)

_failures: list[str] = []


def step(number: int, title: str) -> None:
    print(f"\n{DIM}{'─' * 66}{RESET}")
    print(f"STEP {number}: {title}")
    print(f"{DIM}{'─' * 66}{RESET}")


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    if not ok:
        _failures.append(label)
    return ok


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    api = f"{base}/api/v1"
    client = httpx.Client(timeout=30.0)

    try:
        health = client.get(f"{base}/health")
    except httpx.ConnectError:
        print(f"{RED}Cannot reach {base}. Start it with:{RESET}")
        print("  uvicorn app.main:app --port 8000")
        return 2

    print(f"Target: {base}   mode={health.json().get('pinch_mode')}")

    # ---------------------------------------------------------------- 1
    step(1, "POST /sim/reset")
    reset = client.post(f"{api}/sim/reset")
    check("reset returns 200", reset.status_code == 200, f"HTTP {reset.status_code}")
    check("clock offset back to zero", health.json().get("clock_offset_seconds") == 0)

    # ---------------------------------------------------------------- 2
    step(2, "POST /sim/seed-demo")
    seeded = client.post(f"{api}/sim/seed-demo")
    check("seed returns 200", seeded.status_code == 200)
    summary = seeded.json()

    print(
        f"\n  {summary['seeded']} failed payments across "
        f"{summary['customers']} customers"
    )
    print(f"  AT RISK: {money(summary['at_risk_cents'])}\n")
    print(f"  {'failure_class':24s} {'n':>3s} {'amount':>12s}")
    print(f"  {DIM}{'-' * 44}{RESET}")
    for entry in summary["by_class"]:
        print(
            f"  {entry['failure_class']:24s} {entry['count']:3d} "
            f"{money(entry['amount_cents']):>12s}"
        )
    print()

    check("seeded ~50 payments", 45 <= summary["seeded"] <= 55, str(summary["seeded"]))
    check("by_class counts sum to total",
          sum(e["count"] for e in summary["by_class"]) == summary["seeded"])
    check("by_class amounts sum to at_risk_cents",
          sum(e["amount_cents"] for e in summary["by_class"])
          == summary["at_risk_cents"])

    # ---------------------------------------------------------------- 3
    step(3, "GET /api/v1/payments — contract shape")
    listing = client.get(f"{api}/payments", params={"limit": 200})
    check("payments returns 200", listing.status_code == 200)
    body = listing.json()

    check("response has a data array", isinstance(body.get("data"), list))
    rows = body.get("data", [])
    check("all seeded payments readable", len(rows) == summary["seeded"],
          f"{len(rows)} rows")

    if rows:
        sample = rows[0]
        missing = CONTRACT_PAYMENT_FIELDS - set(sample)
        extra = set(sample) - CONTRACT_PAYMENT_FIELDS
        check("every contract field present", not missing,
              f"missing: {sorted(missing)}" if missing else "")
        if extra:
            print(f"  {YELLOW}note{RESET} extra fields beyond contract: "
                  f"{sorted(extra)}")

        check("amount_cents is an integer", isinstance(sample["amount_cents"], int),
              f"{sample['amount_cents']!r}")
        check("no float money anywhere",
              all(isinstance(r["amount_cents"], int) for r in rows))
        check("currency is AUD", all(r["currency"] == "AUD" for r in rows))
        check("failed_at ends in Z, as the contract shows",
              all((r["failed_at"] or "Z").endswith("Z") for r in rows),
              sample["failed_at"])
        check("raw_code populated (ingest preserved Pinch's code)",
              all(r["raw_code"] for r in rows))
        check("attempts is a list", isinstance(sample["attempts"], list))
        check("customer_name resolved", bool(sample["customer_name"]),
              sample["customer_name"])

        unclassified = [r for r in rows if r["failure_class"] is None]
        print(
            f"  {YELLOW}note{RESET} failure_class NULL on "
            f"{len(unclassified)}/{len(rows)} — expected until Person B's "
            f"classifier runs"
        )

        # Single-payment fetch is what the drill-down view uses.
        one = client.get(f"{api}/payments/{sample['id']}")
        check("GET /payments/{id} returns 200", one.status_code == 200)
        check("single payment has the same shape",
              not (CONTRACT_PAYMENT_FIELDS - set(one.json())))

        missing_404 = client.get(f"{api}/payments/pay_doesnotexist")
        check("unknown id returns 404 in contract error shape",
              missing_404.status_code == 404
              and set(missing_404.json().get("error", {})) == {"code", "message"})

    # ---------------------------------------------------------------- 4
    step(4, "POST /sim/scenarios with webhook_deliveries=2 — idempotency")
    before = len(client.get(f"{api}/payments", params={"limit": 200}).json()["data"])

    dupe = client.post(
        f"{api}/sim/scenarios",
        json={
            "customer_name": "Idempotency Test Co",
            "amount_cents": 24900,
            "outcome": "dishonour",
            "raw_code": "insufficient-funds",
            "delay_seconds": 0,
            "webhook_deliveries": 2,
        },
    )
    check("scenario returns 200", dupe.status_code == 200)
    created = dupe.json()

    results = [d["result"] for d in created.get("deliveries", [])]
    check("event delivered twice", len(results) == 2, str(results))
    check("second delivery rejected as duplicate", results == ["accepted", "duplicate"])

    after_rows = client.get(f"{api}/payments", params={"limit": 200}).json()["data"]
    matching = [r for r in after_rows if r["id"] == created["payment_id"]]
    check("exactly ONE payment row for the duplicated event", len(matching) == 1,
          f"{len(matching)} rows")
    check("ledger grew by exactly one", len(after_rows) == before + 1,
          f"{before} -> {len(after_rows)}")

    # ---------------------------------------------------------------- 5
    step(5, "POST /sim/fast-forward — delayed dishonour becomes visible")
    delayed = client.post(
        f"{api}/sim/scenarios",
        json={
            "customer_name": "Settlement Window Pty Ltd",
            "amount_cents": 39900,
            "outcome": "dishonour",
            "raw_code": "insufficient-funds",
            "delay_seconds": THREE_DAYS,
            "webhook_deliveries": 1,
        },
    ).json()

    check("scenario scheduled, not delivered", delayed["delivered"] is False,
          f"due {delayed['scheduled_for']}")

    rows_now = client.get(f"{api}/payments", params={"limit": 200}).json()["data"]
    check("delayed payment NOT yet visible",
          delayed["payment_id"] not in {r["id"] for r in rows_now})

    forwarded = client.post(f"{api}/sim/fast-forward", json={"seconds": THREE_DAYS})
    check("fast-forward returns 200", forwarded.status_code == 200)
    ff = forwarded.json()
    check("clock advanced three days", ff["clock_offset_seconds"] == THREE_DAYS,
          f"offset {ff['clock_offset_seconds']}s")
    check("the delayed webhook fired", len(ff["webhooks_delivered"]) == 1,
          str([d["result"] for d in ff["webhooks_delivered"]]))

    rows_after = client.get(f"{api}/payments", params={"limit": 200}).json()["data"]
    landed = [r for r in rows_after if r["id"] == delayed["payment_id"]]
    check("delayed payment NOW visible", len(landed) == 1)
    if landed:
        check("it landed as failed with its code",
              landed[0]["status"] == "failed"
              and landed[0]["amount_cents"] == 39900,
              f"{landed[0]['raw_code']} {money(landed[0]['amount_cents'])}")

    # ---------------------------------------------------------------- done
    print(f"\n{DIM}{'═' * 66}{RESET}")
    if _failures:
        print(f"{RED}HANDOFF CHECK FAILED{RESET} — {len(_failures)} problem(s):")
        for name in _failures:
            print(f"  - {name}")
        return 1

    print(f"{GREEN}HANDOFF CHECK PASSED{RESET}")
    print("Person B can build against these endpoints.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
