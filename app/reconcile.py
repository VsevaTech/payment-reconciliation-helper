"""Deterministic reconciliation engine.

Rules (in order):

1. Rows with an empty reference or unparseable amount → ``INVALID_ROW``.
2. Orders sharing a reference: the first row (by row order) is canonical,
   every further row → ``DUPLICATE_ORDER``.
3. Payments sharing a reference: the first row (by payment date, then row
   order) is canonical, every further row → ``DUPLICATE_PAYMENT``.
4. Canonical order ↔ canonical payment by exact reference:
   - currencies differ → ``CURRENCY_MISMATCH`` (amounts are not compared across
     currencies);
   - |order.amount − payment.amount| > tolerance → ``AMOUNT_MISMATCH``;
   - otherwise → ``MATCHED``.
5. Canonical order without payment → ``MISSING_PAYMENT``;
   canonical payment without order → ``ORPHAN_PAYMENT``.
6. Optional fallback (off by default): remaining MISSING/ORPHAN pairs with the
   same amount and currency and a date within *N* days are paired one-to-one in
   row order → ``FALLBACK_MATCHED``. Never affects rules 1–5.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

import pandas as pd

from app.models import (
    Category,
    ColumnMapping,
    ReconcileOptions,
    ReconcileResult,
    Record,
    ResultRow,
)
from app.normalize import build_records


def _fmt(d: Decimal | None) -> str:
    return "" if d is None else format(d, "f")


def _row_from_pair(
    order: Record | None, payment: Record | None, category: Category, explanation: str
) -> ResultRow:
    diff = None
    if order and payment and order.amount is not None and payment.amount is not None:
        diff = payment.amount - order.amount
    return ResultRow(
        category=category,
        explanation=explanation,
        order_row=order.row_number if order else None,
        payment_row=payment.row_number if payment else None,
        reference=(order or payment).reference_raw.strip() if (order or payment) else "",
        order_amount=order.amount if order else None,
        payment_amount=payment.amount if payment else None,
        difference=diff,
        order_currency=order.currency if order else "",
        payment_currency=payment.currency if payment else "",
        order_date=order.date_raw if order else "",
        payment_date=payment.date_raw if payment else "",
    )


def _split_duplicates(
    records: list[Record], *, sort_by_date: bool
) -> tuple[dict[str, Record], list[tuple[Record, Record]]]:
    """Return canonical records by reference and (canonical, duplicate) pairs."""
    groups: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        groups[r.reference].append(r)
    canonical: dict[str, Record] = {}
    duplicates: list[tuple[Record, Record]] = []
    for ref, group in groups.items():
        if sort_by_date:
            group = sorted(group, key=lambda r: (r.date or date.max, r.row_number))
        else:
            group = sorted(group, key=lambda r: r.row_number)
        canonical[ref] = group[0]
        for dup in group[1:]:
            duplicates.append((group[0], dup))
    return canonical, duplicates


def reconcile_records(
    orders: list[Record], payments: list[Record], options: ReconcileOptions | None = None
) -> ReconcileResult:
    options = options or ReconcileOptions()
    rows: list[ResultRow] = []

    valid_orders = [o for o in orders if o.error is None]
    valid_payments = [p for p in payments if p.error is None]
    for o in orders:
        if o.error:
            rows.append(
                _row_from_pair(
                    o, None, Category.INVALID_ROW, f"Order row {o.row_number}: {o.error}"
                )
            )
    for p in payments:
        if p.error:
            rows.append(
                _row_from_pair(
                    None, p, Category.INVALID_ROW, f"Payment row {p.row_number}: {p.error}"
                )
            )

    canon_orders, dup_orders = _split_duplicates(valid_orders, sort_by_date=False)
    canon_payments, dup_payments = _split_duplicates(valid_payments, sort_by_date=True)

    for first, dup in dup_orders:
        rows.append(
            _row_from_pair(
                dup,
                None,
                Category.DUPLICATE_ORDER,
                f"Reference already seen in order row {first.row_number}",
            )
        )
    for first, dup in dup_payments:
        rows.append(
            _row_from_pair(
                None,
                dup,
                Category.DUPLICATE_PAYMENT,
                f"Reference already paid in payment row {first.row_number}",
            )
        )

    unmatched_orders: list[Record] = []
    unmatched_payments: list[Record] = []

    for ref, order in canon_orders.items():
        payment = canon_payments.get(ref)
        if payment is None:
            unmatched_orders.append(order)
            continue
        if order.currency != payment.currency:
            rows.append(
                _row_from_pair(
                    order,
                    payment,
                    Category.CURRENCY_MISMATCH,
                    f"Order currency {order.currency or '∅'} ≠ payment currency "
                    f"{payment.currency or '∅'}",
                )
            )
            continue
        assert order.amount is not None and payment.amount is not None
        diff = payment.amount - order.amount
        if abs(diff) > options.amount_tolerance:
            rows.append(
                _row_from_pair(
                    order,
                    payment,
                    Category.AMOUNT_MISMATCH,
                    f"Paid {_fmt(payment.amount)} vs ordered {_fmt(order.amount)} "
                    f"(diff {'+' if diff > 0 else ''}{_fmt(diff)})",
                )
            )
            continue
        rows.append(
            _row_from_pair(order, payment, Category.MATCHED, "Reference, amount and currency agree")
        )

    for ref, payment in canon_payments.items():
        if ref not in canon_orders:
            unmatched_payments.append(payment)

    if options.enable_fallback_matching:
        unmatched_orders, unmatched_payments, fallback_rows = _fallback_match(
            unmatched_orders, unmatched_payments, options.fallback_date_window_days
        )
        rows.extend(fallback_rows)

    for order in unmatched_orders:
        rows.append(
            _row_from_pair(
                order, None, Category.MISSING_PAYMENT, "No payment carries this reference"
            )
        )
    for payment in unmatched_payments:
        rows.append(
            _row_from_pair(
                None, payment, Category.ORPHAN_PAYMENT, "No order carries this reference"
            )
        )

    rows.sort(key=_sort_key)
    return ReconcileResult(
        rows=rows, orders_total=len(orders), payments_total=len(payments), options=options
    )


def _fallback_match(
    orders: list[Record], payments: list[Record], window_days: int
) -> tuple[list[Record], list[Record], list[ResultRow]]:
    """Greedy one-to-one pairing on (amount, currency) within a date window.

    Deterministic: candidates are scanned in row order; the first payment that
    fits is taken. Rows without a parseable date on either side never match.
    """
    rows: list[ResultRow] = []
    used: set[int] = set()
    left_orders: list[Record] = []
    for order in sorted(orders, key=lambda r: r.row_number):
        chosen = None
        if order.date is not None:
            for payment in sorted(payments, key=lambda r: r.row_number):
                if payment.row_number in used or payment.date is None:
                    continue
                if payment.currency != order.currency or payment.amount != order.amount:
                    continue
                if abs((payment.date - order.date).days) <= window_days:
                    chosen = payment
                    break
        if chosen is None:
            left_orders.append(order)
            continue
        used.add(chosen.row_number)
        rows.append(
            _row_from_pair(
                order,
                chosen,
                Category.FALLBACK_MATCHED,
                f"References differ ({order.reference_raw.strip()} vs "
                f"{chosen.reference_raw.strip()}) but amount, currency and date "
                f"(±{window_days}d) agree — review manually",
            )
        )
    left_payments = [p for p in payments if p.row_number not in used]
    return left_orders, left_payments, rows


_CAT_RANK = {c: i for i, c in enumerate(Category)}


def _sort_key(r: ResultRow) -> tuple[int, int, int]:
    return (_CAT_RANK[r.category], r.order_row or 10**9, r.payment_row or 10**9)


def reconcile_frames(
    orders_df: pd.DataFrame,
    payments_df: pd.DataFrame,
    orders_mapping: ColumnMapping,
    payments_mapping: ColumnMapping,
    options: ReconcileOptions | None = None,
) -> ReconcileResult:
    options = options or ReconcileOptions()
    orders = build_records(
        orders_df, orders_mapping, case_insensitive_reference=options.case_insensitive_reference
    )
    payments = build_records(
        payments_df, payments_mapping, case_insensitive_reference=options.case_insensitive_reference
    )
    return reconcile_records(orders, payments, options)
