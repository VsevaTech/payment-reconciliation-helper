"""Deterministic synthetic dataset generator.

Produces ``orders.csv`` (UTF-8, comma, ISO dates, amounts like ``1234.50``),
``payments.csv`` (UTF-8, semicolon, CRLF, PSP-style columns incl. a
``transaction_type`` column with refunds, voids and chargebacks, shuffled rows) and
``expected.json`` with the exact category counts and per-currency money totals the engine
must report.

The expectations are derived here from what was planted — independently of the
engine — so the tests compare two separate computations.

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
N_SPLIT = 4  # one of them is a three-way split
N_PARTIAL = 2
N_PARTIAL_REFUND = 3
N_FULL_REFUND = 2
N_VOIDED = 1

# Hand-made, easy-to-read cases appended after the random orders (all 100.00 AED).
SHOWCASE = {
    "ORD-2024-01001": "MATCHED",  # 100 capture
    "ORD-2024-01002": "MATCHED_SPLIT",  # 60 + 40
    "ORD-2024-01003": "PARTIAL_PAYMENT",  # 60 + 35
    "ORD-2024-01004": "PARTIALLY_REFUNDED",  # 100 capture, 30 refund
    "ORD-2024-01005": "REFUNDED",  # 100 capture, 100 refund
    "ORD-2024-01006": "POSSIBLE_DUPLICATE_CAPTURE",  # 100 + 100
    "ORD-2024-01007": "CHARGED_BACK",  # 100 capture, 100 chargeback
    "ORD-2024-01008": "CHARGEBACK_REVERSED",  # 100 capture, 100 chargeback, 100 reversal
    "ORD-2024-01009": "CHARGEBACK_EXCEEDS_CAPTURE",  # 100 capture, 100 refund, 100 chargeback
    "ORD-2024-01010": "REVERSAL_EXCEEDS_CHARGEBACK",  # 100 capture, 100 reversal, no chargeback
}
ORPHAN_REFUND_REF = "ORD-2024-95000"

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


CENT = Decimal("0.01")


def split_amount(total: Decimal, ratio: str) -> tuple[Decimal, Decimal]:
    first = (total * Decimal(ratio)).quantize(CENT)
    return first, total - first


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
    split = sorted(take(N_SPLIT))
    partial = set(take(N_PARTIAL))
    partial_refund = set(take(N_PARTIAL_REFUND))
    full_refund = set(take(N_FULL_REFUND))
    voided = set(take(N_VOIDED))

    for k, ref in enumerate(SHOWCASE):
        orders.append(
            {
                "Order ID": ref,
                "Customer": CUSTOMERS[k],
                "Amount": "100.00",
                "Currency": "AED",
                "Date": (start + timedelta(days=20 + k)).isoformat(),
                "Channel": "web",
            }
        )
    by_ref = {o["Order ID"]: i for i, o in enumerate(orders)}

    payments: list[dict[str, str]] = []
    txn = 700000
    # Independent expectations, accumulated while planting (Decimal only).
    fin: dict[str, dict[str, Decimal]] = {}

    def acc(currency: str, key: str, value: Decimal) -> None:
        bucket = fin.setdefault(
            currency,
            {
                k: Decimal("0")
                for k in (
                    "orders_count",
                    "orders_total",
                    "captured",
                    "refunded",
                    "charged_back",
                    "chargeback_reversed",
                    "voided",
                    "unreconciled",
                )
            },
        )
        bucket[key] += value

    def psp_row(
        o: dict[str, str],
        amount: Decimal,
        currency: str,
        day_shift: int = 0,
        kind: str = "CAPTURE",
    ) -> dict[str, str]:
        nonlocal txn
        txn += rng.randint(1, 9)
        d = date.fromisoformat(o["Date"]) + timedelta(days=day_shift)
        # PSP portals export money leaving the merchant as negative amounts.
        signed = -amount if kind in ("REFUND", "CHARGEBACK") else amount
        acc(
            currency,
            {
                "CAPTURE": "captured",
                "REFUND": "refunded",
                "VOID": "voided",
                "CHARGEBACK": "charged_back",
                "CHARGEBACK_REVERSAL": "chargeback_reversed",
            }[kind],
            amount,
        )
        return {
            "psp_transaction_id": f"TXN{txn}",
            "merchant_reference": o["Order ID"],
            "amount": format(signed, "f"),
            "currency": currency,
            "payment_date": d.isoformat(),
            "status": {
                "CAPTURE": "CAPTURED",
                "REFUND": "REFUNDED",
                "VOID": "VOIDED",
                "CHARGEBACK": "CHARGEBACK",
                "CHARGEBACK_REVERSAL": "CHARGEBACK_REVERSED",
            }[kind],
            "transaction_type": kind,
            "card_brand": rng.choice(CARD_BRANDS),
            "merchant_name": rng.choice(CUSTOMERS),
        }

    split_ratios = ["0.60", "0.25", "0.50", "0.30"]
    refund_ratios = ["0.30", "0.10", "0.50"]
    for i, o in enumerate(orders[:N_ORDERS]):
        amount, currency = Decimal(o["Amount"]), o["Currency"]
        acc(currency, "orders_total", amount)
        acc(currency, "orders_count", Decimal(1))
        if i in missing:
            acc(currency, "unreconciled", amount)
            continue
        if i in voided:
            payments.append(psp_row(o, amount, currency, 0, "VOID"))
            acc(currency, "unreconciled", amount)
            continue
        if i in split:
            k = split.index(i)
            a, rest = split_amount(amount, split_ratios[k])
            if k == 0:  # three-way split
                b, c = split_amount(rest, "0.50")
                parts = [a, b, c]
            else:
                parts = [a, rest]
            for n, part in enumerate(parts):
                payments.append(psp_row(o, part, currency, n))
            continue
        if i in partial:
            short = rng.choice([Decimal("0.50"), Decimal("1.00"), Decimal("5.00")])
            a, b = split_amount(amount - short, "0.60")
            payments.append(psp_row(o, a, currency, 0))
            payments.append(psp_row(o, b, currency, 1))
            acc(currency, "unreconciled", short)
            continue
        if i in amount_mismatch:
            delta = rng.choice([Decimal("0.01"), Decimal("-0.10"), Decimal("10.00")])
            payments.append(psp_row(o, amount + delta, currency, rng.randint(0, 2)))
            acc(currency, "unreconciled", abs(delta))
            continue
        if i in currency_mismatch:
            pay_currency = "USD" if currency != "USD" else "AED"
            payments.append(psp_row(o, amount, pay_currency, rng.randint(0, 2)))
            acc(currency, "unreconciled", amount)
            acc(pay_currency, "unreconciled", amount)
            continue
        payments.append(psp_row(o, amount, currency, rng.randint(0, 2)))
        if i in dup_payments:
            payments.append(psp_row(o, amount, currency, rng.randint(0, 1)))
            acc(currency, "unreconciled", amount)
        elif i in partial_refund:
            refund = (amount * Decimal(refund_ratios.pop(0))).quantize(CENT)
            payments.append(psp_row(o, refund, currency, rng.randint(3, 10), "REFUND"))
        elif i in full_refund:
            payments.append(psp_row(o, amount, currency, rng.randint(3, 10), "REFUND"))

    # Showcase orders: fixed, human-readable amounts.
    h = Decimal("100.00")
    show = {ref: orders[by_ref[ref]] for ref in SHOWCASE}
    acc("AED", "orders_total", h * len(SHOWCASE))
    acc("AED", "orders_count", Decimal(len(SHOWCASE)))
    # (the chargeback showcase payments are generated after the shuffle, see below)
    payments.append(psp_row(show["ORD-2024-01001"], h, "AED"))
    payments.append(psp_row(show["ORD-2024-01002"], Decimal("60.00"), "AED"))
    payments.append(psp_row(show["ORD-2024-01002"], Decimal("40.00"), "AED", 1))
    payments.append(psp_row(show["ORD-2024-01003"], Decimal("60.00"), "AED"))
    payments.append(psp_row(show["ORD-2024-01003"], Decimal("35.00"), "AED", 1))
    acc("AED", "unreconciled", Decimal("5.00"))
    payments.append(psp_row(show["ORD-2024-01004"], h, "AED"))
    payments.append(psp_row(show["ORD-2024-01004"], Decimal("30.00"), "AED", 5, "REFUND"))
    payments.append(psp_row(show["ORD-2024-01005"], h, "AED"))
    payments.append(psp_row(show["ORD-2024-01005"], h, "AED", 6, "REFUND"))
    payments.append(psp_row(show["ORD-2024-01006"], h, "AED"))
    payments.append(psp_row(show["ORD-2024-01006"], h, "AED", 0))
    acc("AED", "unreconciled", h)

    def ex(captured, net, diff="0.00", refunded="0", cb="0", rev="0"):
        return {
            "captured": captured,
            "refunded": refunded,
            "chargeback": cb,
            "chargeback_reversed": rev,
            "net": net,
            "diff": diff,
        }

    showcase_expect = {
        "ORD-2024-01001": ex("100.00", "100.00"),
        "ORD-2024-01002": ex("100.00", "100.00"),
        "ORD-2024-01003": ex("95.00", "95.00", diff="-5.00"),
        "ORD-2024-01004": ex("100.00", "70.00", refunded="30.00"),
        "ORD-2024-01005": ex("100.00", "0.00", refunded="100.00"),
        "ORD-2024-01006": ex("200.00", "200.00", diff="100.00"),
        "ORD-2024-01007": ex("100.00", "0.00", cb="100.00"),
        "ORD-2024-01008": ex("100.00", "100.00", cb="100.00", rev="100.00"),
        "ORD-2024-01009": ex("100.00", "-100.00", refunded="100.00", cb="100.00"),
        "ORD-2024-01010": ex("100.00", "200.00", rev="100.00"),
    }

    for k in range(N_ORPHANS):
        fake = {
            "Order ID": f"ORD-2024-9{k:04d}",
            "Date": (start + timedelta(days=k * 3)).isoformat(),
        }
        amount, currency = money(rng), rng.choice(CURRENCIES)
        payments.append(psp_row(fake, amount, currency))
        acc(currency, "unreconciled", amount)
    fake = {"Order ID": ORPHAN_REFUND_REF, "Date": (start + timedelta(days=25)).isoformat()}
    payments.append(psp_row(fake, Decimal("25.00"), "AED", 0, "REFUND"))
    acc("AED", "unreconciled", Decimal("25.00"))

    for i in sorted(dup_orders):
        orders.append(dict(orders[i]))

    rng.shuffle(payments)

    # Chargeback showcase (added in 0.3). Generated after the shuffle and inserted
    # at seeded positions, so every earlier random draw — and the relative order of
    # all 0.2 rows — is unchanged and the dataset diff stays reviewable.
    chargeback_rows: list[dict[str, str]] = []
    chargeback_rows.append(psp_row(show["ORD-2024-01007"], h, "AED"))
    chargeback_rows.append(psp_row(show["ORD-2024-01007"], h, "AED", 30, "CHARGEBACK"))
    chargeback_rows.append(psp_row(show["ORD-2024-01008"], h, "AED"))
    chargeback_rows.append(psp_row(show["ORD-2024-01008"], h, "AED", 25, "CHARGEBACK"))
    chargeback_rows.append(psp_row(show["ORD-2024-01008"], h, "AED", 50, "CHARGEBACK_REVERSAL"))
    chargeback_rows.append(psp_row(show["ORD-2024-01009"], h, "AED"))
    chargeback_rows.append(psp_row(show["ORD-2024-01009"], h, "AED", 3, "REFUND"))
    chargeback_rows.append(psp_row(show["ORD-2024-01009"], h, "AED", 28, "CHARGEBACK"))
    acc("AED", "unreconciled", h)  # the cardholder got 100.00 back twice
    chargeback_rows.append(psp_row(show["ORD-2024-01010"], h, "AED"))
    chargeback_rows.append(psp_row(show["ORD-2024-01010"], h, "AED", 40, "CHARGEBACK_REVERSAL"))
    acc("AED", "unreconciled", h)  # reversal of a chargeback that never happened

    for row in chargeback_rows:
        payments.insert(rng.randint(0, len(payments)), row)

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

    sc = list(SHOWCASE.values())
    showcase_counts = {c: sc.count(c) for c in set(sc)}
    random_special = (
        N_MISSING
        + N_AMOUNT_MISMATCH
        + N_CURRENCY_MISMATCH
        + N_DUP_PAYMENTS
        + N_SPLIT
        + N_PARTIAL
        + N_PARTIAL_REFUND
        + N_FULL_REFUND
        + N_VOIDED
    )
    summary = {
        "MATCHED": N_ORDERS - random_special + showcase_counts["MATCHED"],
        "MATCHED_SPLIT": N_SPLIT + showcase_counts["MATCHED_SPLIT"],
        "PARTIALLY_REFUNDED": N_PARTIAL_REFUND + showcase_counts["PARTIALLY_REFUNDED"],
        "REFUNDED": N_FULL_REFUND + showcase_counts["REFUNDED"],
        "CHARGEBACK_REVERSED": showcase_counts["CHARGEBACK_REVERSED"],
        "MISSING_PAYMENT": N_MISSING,
        "ORPHAN_PAYMENT": N_ORPHANS + 1,
        "AMOUNT_MISMATCH": N_AMOUNT_MISMATCH,
        "PARTIAL_PAYMENT": N_PARTIAL + showcase_counts["PARTIAL_PAYMENT"],
        "CURRENCY_MISMATCH": N_CURRENCY_MISMATCH,
        "POSSIBLE_DUPLICATE_CAPTURE": N_DUP_PAYMENTS
        + showcase_counts["POSSIBLE_DUPLICATE_CAPTURE"],
        "REFUND_EXCEEDS_CAPTURE": 0,
        "CHARGED_BACK": showcase_counts["CHARGED_BACK"],
        "CHARGEBACK_EXCEEDS_CAPTURE": showcase_counts["CHARGEBACK_EXCEEDS_CAPTURE"],
        "REVERSAL_EXCEEDS_CHARGEBACK": showcase_counts["REVERSAL_EXCEEDS_CHARGEBACK"],
        "VOIDED": N_VOIDED,
        "DUPLICATE_ORDER": N_DUP_ORDERS,
        "FALLBACK_MATCHED": 0,
        "INVALID_ROW": 0,
    }
    # Without the transaction_type column every row is a capture with its signed
    # amount: refund groups contain a negative amount → AMOUNT_MISMATCH, and a
    # lone VOID row "pays" its order → MATCHED. This is why the column matters.
    refund_groups = summary["PARTIALLY_REFUNDED"] + summary["REFUNDED"]
    untyped = dict(summary)
    untyped["MATCHED"] += N_VOIDED
    untyped["AMOUNT_MISMATCH"] += refund_groups
    untyped["PARTIALLY_REFUNDED"] = untyped["REFUNDED"] = untyped["VOIDED"] = 0
    # Chargebacks are negative too → AMOUNT_MISMATCH; a lone reversal (+100 on a
    # 100 capture) looks like a second capture → POSSIBLE_DUPLICATE_CAPTURE.
    chargeback_negative = ("CHARGED_BACK", "CHARGEBACK_REVERSED", "CHARGEBACK_EXCEEDS_CAPTURE")
    untyped["AMOUNT_MISMATCH"] += sum(summary[c] for c in chargeback_negative)
    untyped["POSSIBLE_DUPLICATE_CAPTURE"] += summary["REVERSAL_EXCEEDS_CHARGEBACK"]
    for c in (*chargeback_negative, "REVERSAL_EXCEEDS_CHARGEBACK"):
        untyped[c] = 0

    def refs(indices: set[int] | list[int]) -> list[str]:
        return sorted(orders[i]["Order ID"] for i in indices)

    def showcase(category: str) -> list[str]:
        return [r for r, c in SHOWCASE.items() if c == category]

    expected = {
        "orders_rows": len(orders),
        "payments_rows": len(payments),
        "summary": summary,
        "summary_without_transaction_type": untyped,
        "financials": [
            {
                "currency": c,
                "orders_count": int(v["orders_count"]),
                "orders_total": format(v["orders_total"], "f"),
                "captured": format(v["captured"], "f"),
                "refunded": format(v["refunded"], "f"),
                "charged_back": format(v["charged_back"], "f"),
                "chargeback_reversed": format(v["chargeback_reversed"], "f"),
                "net_captured": format(
                    v["captured"] - v["refunded"] - v["charged_back"] + v["chargeback_reversed"],
                    "f",
                ),
                "voided": format(v["voided"], "f"),
                "unreconciled": format(v["unreconciled"], "f"),
            }
            for c, v in sorted(fin.items())
        ],
        "planted": {
            "missing_payment": refs(missing),
            "amount_mismatch": refs(amount_mismatch),
            "currency_mismatch": refs(currency_mismatch),
            "possible_duplicate_capture": sorted(
                refs(dup_payments) + showcase("POSSIBLE_DUPLICATE_CAPTURE")
            ),
            "duplicate_order": refs(dup_orders),
            "matched_split": sorted(refs(split) + showcase("MATCHED_SPLIT")),
            "partial_payment": sorted(refs(partial) + showcase("PARTIAL_PAYMENT")),
            "partially_refunded": sorted(refs(partial_refund) + showcase("PARTIALLY_REFUNDED")),
            "refunded": sorted(refs(full_refund) + showcase("REFUNDED")),
            "voided": refs(voided),
            "charged_back": showcase("CHARGED_BACK"),
            "chargeback_reversed": showcase("CHARGEBACK_REVERSED"),
            "chargeback_exceeds_capture": showcase("CHARGEBACK_EXCEEDS_CAPTURE"),
            "reversal_exceeds_chargeback": showcase("REVERSAL_EXCEEDS_CHARGEBACK"),
            "orphan_payment": sorted(
                p["merchant_reference"]
                for p in payments
                if p["merchant_reference"].startswith("ORD-2024-9")
            ),
        },
        "showcase": {ref: {"category": SHOWCASE[ref], **showcase_expect[ref]} for ref in SHOWCASE},
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
            "transaction_type": "transaction_type",
        },
    }
    (OUT / "expected.json").write_text(
        json.dumps(expected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(expected["summary"], indent=2))


if __name__ == "__main__":
    main()
