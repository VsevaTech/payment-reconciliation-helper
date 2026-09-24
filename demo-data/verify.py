"""Check a reconciliation payload against ``expected.json``.

The payload is what ``POST /api/reconcile`` (with ``include_rows=true``) or
``python -m app.cli … --json-full`` returns. Used by CI and for manual demos:

    python -m app.cli demo-data/orders.csv demo-data/payments.csv --json-full > out.json
    python demo-data/verify.py out.json
    python demo-data/verify.py out.json --without-transaction-type   # legacy mode run

Exits non-zero with a readable message on the first mismatch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

PLANTED = {
    "missing_payment": "MISSING_PAYMENT",
    "amount_mismatch": "AMOUNT_MISMATCH",
    "currency_mismatch": "CURRENCY_MISMATCH",
    "possible_duplicate_capture": "POSSIBLE_DUPLICATE_CAPTURE",
    "duplicate_order": "DUPLICATE_ORDER",
    "matched_split": "MATCHED_SPLIT",
    "partial_payment": "PARTIAL_PAYMENT",
    "partially_refunded": "PARTIALLY_REFUNDED",
    "refunded": "REFUNDED",
    "voided": "VOIDED",
    "charged_back": "CHARGED_BACK",
    "chargeback_reversed": "CHARGEBACK_REVERSED",
    "chargeback_exceeds_capture": "CHARGEBACK_EXCEEDS_CAPTURE",
    "reversal_exceeds_chargeback": "REVERSAL_EXCEEDS_CHARGEBACK",
    "orphan_payment": "ORPHAN_PAYMENT",
}


def fail(msg: str) -> None:
    sys.exit(f"demo verification FAILED: {msg}")


def verify(payload: dict, expected: dict, *, typed: bool = True) -> list[str]:
    checks: list[str] = []
    want = expected["summary"] if typed else expected["summary_without_transaction_type"]
    if payload["summary"] != want:
        diff = {
            k: (payload["summary"].get(k), v)
            for k, v in want.items()
            if payload["summary"].get(k) != v
        }
        fail(f"summary differs (got, expected): {diff}")
    checks.append("category summary")
    if payload.get("transaction_type_mapped") is not typed:
        fail(f"transaction_type_mapped should be {typed}")

    rows = payload.get("rows")
    if rows is None:
        fail("payload has no rows (use include_rows=true / --json-full)")

    # Source-row traceability: every input row appears exactly once.
    order_rows = sorted(int(r["order_row"]) for r in rows if r["order_row"] != "")
    pay_rows = sorted(int(n) for r in rows for n in r["payment_rows"].split(";") if n)
    if order_rows != list(range(1, expected["orders_rows"] + 1)):
        fail("order rows are not each reported exactly once")
    if pay_rows != list(range(1, expected["payments_rows"] + 1)):
        fail("payment rows are not each reported exactly once")
    checks.append("every source row reported exactly once")

    if not typed:
        return checks

    if payload["financials"] != expected["financials"]:
        fail(f"financials differ:\n got {payload['financials']}\n exp {expected['financials']}")
    checks.append("per-currency financials")

    for key, category in PLANTED.items():
        got = sorted(r["reference"] for r in rows if r["category"] == category)
        if got != expected["planted"][key]:
            fail(f"{category}: got {got}, expected {expected['planted'][key]}")
    checks.append("planted references land in their categories")

    by_ref = {r["reference"]: r for r in rows if r["order_row"] != ""}
    for ref, exp in expected["showcase"].items():
        r = by_ref[ref]
        got = {
            "category": r["category"],
            "captured": r["captured_amount"],
            "refunded": r["refunded_amount"],
            "chargeback": r["chargeback_amount"],
            "chargeback_reversed": r["chargeback_reversed_amount"],
            "net": r["net_amount"],
            "diff": r["difference"],
        }
        if got != exp:
            fail(f"showcase {ref}: got {got}, expected {exp}")
    checks.append("showcase amounts (split / partial / refunds / chargebacks / duplicate)")
    return checks


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("payload", type=Path)
    p.add_argument("--expected", type=Path, default=HERE / "expected.json")
    p.add_argument("--without-transaction-type", action="store_true")
    args = p.parse_args()
    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    checks = verify(payload, expected, typed=not args.without_transaction_type)
    print("demo verification OK:", "; ".join(checks))
    print(json.dumps(payload["summary"]))


if __name__ == "__main__":
    main()
