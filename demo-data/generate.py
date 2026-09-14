"""Deterministic synthetic dataset generator.

Produces ``orders.csv`` (UTF-8, comma, ISO dates, amounts like ``1234.50``),
``payments.csv`` (UTF-8, semicolon, CRLF, PSP-style columns, shuffled rows) and
``expected.json`` with the exact category counts the engine must report.

Run: ``python demo-data/generate.py`` (idempotent — fixed seed).
"""

from __future__ import annotations

import csv
import io
import json
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

SEED = 20240914
N_ORDERS = 1000
N_MISSING = 6
N_AMOUNT_MISMATCH = 3
N_CURRENCY_MISMATCH = 2
N_DUP_PAYMENTS = 2
N_DUP_ORDERS = 2
N_ORPHANS = 5

CURRENCIES = ["AED"] * 70 + ["USD"] * 15 + ["SAR"] * 10 + ["EUR"] * 5
CHANNELS = ["web", "app", "pos", "link"]
CARD_BRANDS = ["VISA", "MASTERCARD", "MADA", "AMEX"]
CUSTOMERS = [
    "Al Futtaim Trading",
    "Noon Retail",
    "Café Bateel",
    "Zara Home Dubai",
    "Émile Boutique",
    "Lulu Hypermarket",
    "Sharaf DG",
    "Müller & Co",
    "Carrefour Marina",
    "Jumbo Electronics",
    "Al Maya Group",
    "Kibsons",
    "Spinneys Motor City",
    "Home Centre",
    "Nando's JLT",
]
OUT = Path(__file__).resolve().parent


def money(rng: random.Random) -> Decimal:
    cents = rng.choice([0, 0, 0, 25, 50, 75, 99, 5, 10, 95, 49])
    whole = rng.choice([rng.randint(5, 199), rng.randint(200, 2500), rng.randint(2500, 15000)])
    return Decimal(whole) + Decimal(cents) / Decimal(100)


def main() -> None:
    rng = random.Random(SEED)
    start = date(2024, 9, 1)

    orders: list[dict[str, str]] = []
    for i in range(1, N_ORDERS + 1):
        d = start + timedelta(days=rng.randint(0, 29))
        orders.append(
            {
                "Order ID": f"ORD-2024-{i:05d}",
                "Customer": rng.choice(CUSTOMERS),
                "Amount": format(money(rng), "f"),
                "Currency": rng.choice(CURRENCIES),
                "Date": d.isoformat(),
                "Channel": rng.choice(CHANNELS),
            }
        )

    idx = list(range(N_ORDERS))
    rng.shuffle(idx)
    cursor = 0

    def take(n: int) -> list[int]:
        nonlocal cursor
        chunk = idx[cursor : cursor + n]
        cursor += n
        return chunk

    missing = set(take(N_MISSING))
    amount_mismatch = set(take(N_AMOUNT_MISMATCH))
    currency_mismatch = set(take(N_CURRENCY_MISMATCH))
    dup_payments = set(take(N_DUP_PAYMENTS))
    dup_orders = set(take(N_DUP_ORDERS))

    payments: list[dict[str, str]] = []
    txn = 700000

    def psp_row(
        o: dict[str, str], amount: str, currency: str, day_shift: int = 0
    ) -> dict[str, str]:
        nonlocal txn
        txn += rng.randint(1, 9)
        d = date.fromisoformat(o["Date"]) + timedelta(days=day_shift)
        return {
            "psp_transaction_id": f"TXN{txn}",
            "merchant_reference": o["Order ID"],
            "amount": amount,
            "currency": currency,
            "payment_date": d.isoformat(),
            "status": "CAPTURED",
            "card_brand": rng.choice(CARD_BRANDS),
            "merchant_name": rng.choice(CUSTOMERS),
        }

    for i, o in enumerate(orders):
        if i in missing:
            continue
        amount, currency = o["Amount"], o["Currency"]
        if i in amount_mismatch:
            delta = rng.choice([Decimal("0.01"), Decimal("-0.10"), Decimal("10.00")])
            amount = format(Decimal(amount) + delta, "f")
        if i in currency_mismatch:
            currency = "USD" if currency != "USD" else "AED"
        payments.append(psp_row(o, amount, currency, rng.randint(0, 2)))
        if i in dup_payments:
            payments.append(psp_row(o, amount, currency, rng.randint(0, 1)))

    for k in range(N_ORPHANS):
        fake = {
            "Order ID": f"ORD-2024-9{k:04d}",
            "Date": (start + timedelta(days=k * 3)).isoformat(),
        }
        payments.append(psp_row(fake, format(money(rng), "f"), rng.choice(CURRENCIES)))

    for i in sorted(dup_orders):
        orders.append(dict(orders[i]))

    rng.shuffle(payments)

    orders_buf = io.StringIO()
    w = csv.DictWriter(orders_buf, fieldnames=list(orders[0].keys()), lineterminator="\n")
    w.writeheader()
    w.writerows(orders)
    (OUT / "orders.csv").write_bytes(orders_buf.getvalue().encode("utf-8"))

    pay_buf = io.StringIO()
    w = csv.DictWriter(
        pay_buf, fieldnames=list(payments[0].keys()), delimiter=";", lineterminator="\r\n"
    )
    w.writeheader()
    w.writerows(payments)
    # Semicolon + CRLF export typical of PSP portals. Kept UTF-8 so the file is
    # text-safe in git; cp1252 / UTF-16 handling is covered by tests/test_csv_loader.py.
    (OUT / "payments.csv").write_bytes(pay_buf.getvalue().encode("utf-8"))

    matched = N_ORDERS - N_MISSING - N_AMOUNT_MISMATCH - N_CURRENCY_MISMATCH
    expected = {
        "orders_rows": len(orders),
        "payments_rows": len(payments),
        "summary": {
            "MATCHED": matched,
            "MISSING_PAYMENT": N_MISSING,
            "ORPHAN_PAYMENT": N_ORPHANS,
            "AMOUNT_MISMATCH": N_AMOUNT_MISMATCH,
            "CURRENCY_MISMATCH": N_CURRENCY_MISMATCH,
            "DUPLICATE_PAYMENT": N_DUP_PAYMENTS,
            "DUPLICATE_ORDER": N_DUP_ORDERS,
            "FALLBACK_MATCHED": 0,
            "INVALID_ROW": 0,
        },
        "planted": {
            "missing_payment": sorted(orders[i]["Order ID"] for i in missing),
            "amount_mismatch": sorted(orders[i]["Order ID"] for i in amount_mismatch),
            "currency_mismatch": sorted(orders[i]["Order ID"] for i in currency_mismatch),
            "duplicate_payment": sorted(orders[i]["Order ID"] for i in dup_payments),
            "duplicate_order": sorted(orders[i]["Order ID"] for i in dup_orders),
            "orphan_payment": sorted(
                p["merchant_reference"]
                for p in payments
                if p["merchant_reference"].startswith("ORD-2024-9")
            ),
        },
        "orders_mapping": {
            "reference": "Order ID",
            "amount": "Amount",
            "currency": "Currency",
            "date": "Date",
        },
        "payments_mapping": {
            "reference": "merchant_reference",
            "amount": "amount",
            "currency": "currency",
            "date": "payment_date",
        },
    }
    (OUT / "expected.json").write_text(
        json.dumps(expected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(expected["summary"], indent=2))


if __name__ == "__main__":
    main()
