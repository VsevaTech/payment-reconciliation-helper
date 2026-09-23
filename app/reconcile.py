"""Deterministic reconciliation engine.

Rules (in order — see README "Matching rules" for the full table):

1. Rows with an empty reference, an unparseable amount or (when a
   transaction-type column is mapped) an unknown type → ``INVALID_ROW``.
2. Orders sharing a reference: the first row (by row order) is canonical,
   every further row → ``DUPLICATE_ORDER``.
3. All valid payment rows sharing a reference form one *payment group*
   (sorted by payment date, then row order). A group is split by type into
   captures (PAYMENT), refunds (REFUND) and voids (VOID). Without a mapped
   transaction-type column every row is a PAYMENT with its signed amount.
4. Canonical order ↔ payment group by exact reference:
   - any payment currency ≠ order currency → ``CURRENCY_MISMATCH`` (never summed
     across currencies);
   - no captures: only voids → ``VOIDED``; refunds → ``REFUND_EXCEEDS_CAPTURE``;
   - refunded > captured → ``REFUND_EXCEEDS_CAPTURE``;
   - one capture: equal (± tolerance) → ``MATCHED`` else ``AMOUNT_MISMATCH``;
   - several captures: sum equal → ``MATCHED_SPLIT``; sum short →
     ``PARTIAL_PAYMENT``; sum over and the surplus is explained by a repeated
     capture → ``POSSIBLE_DUPLICATE_CAPTURE``; otherwise ``AMOUNT_MISMATCH``;
   - captures reconcile (MATCHED / MATCHED_SPLIT) and something was refunded →
     ``REFUNDED`` (refunded == captured) or ``PARTIALLY_REFUNDED``.
5. Canonical order without payment group → ``MISSING_PAYMENT``; payment group
   without order → one ``ORPHAN_PAYMENT`` row listing all its source rows.
6. Optional fallback (off by default): MISSING orders and single-capture ORPHAN
   groups with the same amount and currency and a date within *N* days are
   paired one-to-one in row order → ``FALLBACK_MATCHED``.

All money is ``Decimal``. Every source row lands in exactly one result row.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

import pandas as pd

from app.models import (
    Category,
    ColumnMapping,
    CurrencyTotals,
    ReconcileOptions,
    ReconcileResult,
    Record,
    ResultRow,
    TxnType,
)
from app.normalize import build_records

ZERO = Decimal("0")


def _fmt(d: Decimal | None) -> str:
    return "" if d is None else format(d, "f")


def _signed(d: Decimal) -> str:
    return f"{'+' if d > 0 else ''}{_fmt(d)}"


def _rows_txt(records: list[Record]) -> str:
    return ", ".join(str(r.row_number) for r in records)


def _date_key(r: Record) -> tuple[date, int]:
    return (r.date or date.max, r.row_number)


def _types_txt(records: list[Record]) -> str:
    counts = Counter(r.txn_type for r in records)
    parts = []
    for t in TxnType:
        n = counts.get(t, 0)
        if n:
            parts.append(t.value if n == 1 else f"{t.value}×{n}")
    return ", ".join(parts)


def _row_from_pair(
    order: Record | None, payment: Record | None, category: Category, explanation: str
) -> ResultRow:
    """Result row for zero or one payment (INVALID / MISSING / DUPLICATE_ORDER / FALLBACK)."""
    diff = None
    if order and payment and order.amount is not None and payment.amount is not None:
        diff = payment.amount - order.amount
    src = order or payment
    return ResultRow(
        category=category,
        explanation=explanation,
        order_row=order.row_number if order else None,
        payment_row=payment.row_number if payment else None,
        payment_rows=[payment.row_number] if payment else [],
        reference=src.reference_raw.strip() if src else "",
        order_amount=order.amount if order else None,
        payment_amount=payment.amount if payment else None,
        captured_amount=(
            payment.amount if payment and payment.txn_type is TxnType.PAYMENT else None
        ),
        difference=diff,
        order_currency=order.currency if order else "",
        payment_currency=payment.currency if payment else "",
        order_date=order.date_raw if order else "",
        payment_date=payment.date_raw if payment else "",
        transaction_types=_types_txt([payment]) if payment else "",
    )


class _GroupMoney:
    """Decimal totals of one payment group in ONE currency."""

    def __init__(self, records: list[Record], typed: bool) -> None:
        self.captures = [r for r in records if r.txn_type is TxnType.PAYMENT]
        self.refunds = [r for r in records if r.txn_type is TxnType.REFUND]
        self.voids = [r for r in records if r.txn_type is TxnType.VOID]
        # Untyped: every row is a PAYMENT with its *signed* amount (legacy behaviour).
        self.captured = sum((r.amount for r in self.captures), ZERO)  # type: ignore[misc]
        self.refunded = sum((r.magnitude for r in self.refunds), ZERO)
        self.voided = sum((r.magnitude for r in self.voids), ZERO)
        self.typed = typed

    @property
    def net(self) -> Decimal:
        return self.captured - self.refunded

    @property
    def refund_excess(self) -> Decimal:
        """Refunded beyond what was captured (0 when there are no refunds)."""
        if not self.refunds:
            return ZERO
        return max(ZERO, self.refunded - self.captured)

    def unreconciled_vs(self, order_amount: Decimal) -> Decimal:
        return abs(self.captured - order_amount) + self.refund_excess


def _group_row(
    category: Category,
    explanation: str,
    order: Record | None,
    group: list[Record],
    money: _GroupMoney | None,
    *,
    duplicates: list[Record] | None = None,
) -> ResultRow:
    primary = next((r for r in group if r.txn_type is TxnType.PAYMENT), group[0])
    currencies = sorted({r.currency for r in group})
    diff = None
    if money is not None and order is not None and order.amount is not None:
        diff = money.captured - order.amount
    typed = money.typed if money else False
    return ResultRow(
        category=category,
        explanation=explanation,
        order_row=order.row_number if order else None,
        payment_row=primary.row_number,
        payment_rows=sorted(r.row_number for r in group),
        reference=(order or primary).reference_raw.strip(),
        order_amount=order.amount if order else None,
        payment_amount=money.captured if money else None,
        captured_amount=money.captured if money else None,
        refunded_amount=money.refunded if money and typed else None,
        voided_amount=money.voided if money and typed else None,
        net_amount=money.net if money else None,
        difference=diff,
        order_currency=order.currency if order else "",
        payment_currency=", ".join(c or "∅" for c in currencies)
        if len(currencies) > 1
        else currencies[0],
        order_date=order.date_raw if order else "",
        payment_date=primary.date_raw,
        transaction_types=_types_txt(group),
        duplicate_payment_rows=sorted(r.row_number for r in duplicates or []),
    )


def _split_orders(records: list[Record]) -> tuple[dict[str, Record], list[tuple[Record, Record]]]:
    groups: dict[str, list[Record]] = defaultdict(list)
    for r in sorted(records, key=lambda r: r.row_number):
        groups[r.reference].append(r)
    canonical = {ref: g[0] for ref, g in groups.items()}
    duplicates = [(g[0], dup) for g in groups.values() for dup in g[1:]]
    return canonical, duplicates


def _payment_groups(records: list[Record]) -> dict[str, list[Record]]:
    groups: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        groups[r.reference].append(r)
    return {ref: sorted(g, key=_date_key) for ref, g in groups.items()}


def _find_duplicate_captures(
    captures: list[Record], order_amount: Decimal, tol: Decimal
) -> tuple[list[Record], str] | None:
    """Explain a capture surplus by repeated captures, or return ``None``.

    1. A single capture alone equals the order → every other capture is suspect.
    2. Otherwise captures repeating an earlier capture's exact amount are
       candidates. The latest candidates are dropped one by one (newest first)
       while the remainder is still above the order; if the remainder then
       equals the order, the dropped captures are the suspects
       (``60 + 40 + 40`` → last 40; ``50 + 50 + 50`` for 100 → last 50).
    """
    exact = [c for c in captures if abs(c.amount - order_amount) <= tol]  # type: ignore[operator]
    if exact:
        original = exact[0]
        suspects = [c for c in captures if c is not original]
        return suspects, f"capture row {original.row_number} alone equals the order"
    seen: dict[Decimal, Record] = {}
    suspects = []
    for c in captures:
        assert c.amount is not None
        if c.amount in seen:
            suspects.append(c)
        else:
            seen[c.amount] = c
    rest = sum((c.amount for c in captures), ZERO)  # type: ignore[misc]
    dropped: list[Record] = []
    for c in reversed(suspects):
        if rest - order_amount <= tol:
            break
        rest -= c.amount  # type: ignore[operator]
        dropped.append(c)
    if dropped and abs(rest - order_amount) <= tol:
        return sorted(dropped, key=_date_key), "the remaining captures equal the order"
    return None


def _evaluate_group(
    order: Record,
    group: list[Record],
    options: ReconcileOptions,
    typed: bool,
    unrec: dict[str, Decimal],
) -> ResultRow:
    assert order.amount is not None
    tol = options.amount_tolerance
    currencies = sorted({r.currency for r in group})

    # --- currency ------------------------------------------------------------
    if currencies != [order.currency]:
        foreign = [c for c in currencies if c != order.currency]
        unrec[order.currency] += abs(order.amount)
        parts = []
        for c in currencies:
            m = _GroupMoney([r for r in group if r.currency == c], typed)
            if c != order.currency:
                unrec[c] += m.unreconciled_vs(ZERO)
            parts.append(f"{c or '∅'} net {_fmt(m.net)}")
        explanation = (
            f"Order currency {order.currency or '∅'} ≠ payment currency "
            f"{', '.join(c or '∅' for c in foreign)}"
        )
        money = None
        if len(currencies) == 1:
            money = _GroupMoney(group, typed)
        else:
            explanation += f" (amounts not summed across currencies: {'; '.join(parts)})"
        row = _group_row(Category.CURRENCY_MISMATCH, explanation, order, group, money)
        row.difference = None  # never compare amounts across currencies
        return row

    m = _GroupMoney(group, typed)
    caps = m.captures
    void_note = (
        f"; void row(s) {_rows_txt(m.voids)} ({_fmt(m.voided)}) not counted as captured"
        if m.voids
        else ""
    )
    refund_note = (
        f"; refunded {_fmt(m.refunded)} in row(s) {_rows_txt(m.refunds)} → net {_fmt(m.net)}"
        if m.refunds
        else ""
    )

    # --- nothing captured ----------------------------------------------------
    if not caps:
        if not m.refunds:
            unrec[order.currency] += abs(order.amount)
            return _group_row(
                Category.VOIDED,
                f"Only VOID transactions (row(s) {_rows_txt(m.voids)}, {_fmt(m.voided)}): "
                f"nothing captured against order {_fmt(order.amount)}",
                order,
                group,
                m,
            )
        unrec[order.currency] += m.unreconciled_vs(order.amount)
        return _group_row(
            Category.REFUND_EXCEEDS_CAPTURE,
            f"Refunded {_fmt(m.refunded)} (row(s) {_rows_txt(m.refunds)}) but nothing was "
            f"captured against order {_fmt(order.amount)}{void_note}",
            order,
            group,
            m,
        )

    if m.refund_excess > 0:
        unrec[order.currency] += m.unreconciled_vs(order.amount)
        return _group_row(
            Category.REFUND_EXCEEDS_CAPTURE,
            f"Refunded {_fmt(m.refunded)} (row(s) {_rows_txt(m.refunds)}) exceeds captured "
            f"{_fmt(m.captured)} (row(s) {_rows_txt(caps)}) by "
            f"{_fmt(m.refunded - m.captured)}{void_note}",
            order,
            group,
            m,
        )

    # --- captures vs order ----------------------------------------------------
    diff = m.captured - order.amount
    duplicates: list[Record] = []
    if len(caps) == 1:
        if abs(diff) <= tol:
            category, explanation = Category.MATCHED, "Reference, amount and currency agree"
        else:
            category = Category.AMOUNT_MISMATCH
            explanation = (
                f"Paid {_fmt(m.captured)} vs ordered {_fmt(order.amount)} (diff {_signed(diff)})"
            )
    else:
        parts = " + ".join(_fmt(c.amount) for c in caps)
        noun = "captures" if typed else "payments"
        desc = f"{len(caps)} {noun} (rows {_rows_txt(caps)}: {parts} = {_fmt(m.captured)})"
        negatives = [c for c in caps if c.amount is not None and c.amount < 0]
        if negatives and not typed:
            category = Category.AMOUNT_MISMATCH
            explanation = (
                f"{desc} include negative amount(s) in row(s) {_rows_txt(negatives)} but no "
                f"transaction-type column is mapped, so refunds are not netted — map "
                f"transaction_type (diff {_signed(diff)})"
            )
        elif abs(diff) <= tol:
            category = Category.MATCHED_SPLIT
            explanation = f"{desc} equal the order amount {_fmt(order.amount)}"
        elif diff < 0:
            category = Category.PARTIAL_PAYMENT
            explanation = (
                f"{desc} are short of the order {_fmt(order.amount)} (diff {_signed(diff)})"
            )
        else:
            found = _find_duplicate_captures(caps, order.amount, tol)
            if found:
                duplicates, why = found
                category = Category.POSSIBLE_DUPLICATE_CAPTURE
                explanation = (
                    f"{desc} exceed the order {_fmt(order.amount)} (diff {_signed(diff)}); "
                    f"{why}, so capture row(s) {_rows_txt(duplicates)} look like a repeated "
                    f"capture"
                )
            else:
                category = Category.AMOUNT_MISMATCH
                explanation = (
                    f"{desc} exceed the order {_fmt(order.amount)} (diff {_signed(diff)}); "
                    f"no repeated capture explains the surplus"
                )

    # --- refunds on top of reconciled captures ---------------------------------
    if category in (Category.MATCHED, Category.MATCHED_SPLIT) and m.refunded > 0:
        how = "split " if category is Category.MATCHED_SPLIT else ""
        if m.refunded == m.captured:
            category = Category.REFUNDED
            explanation = (
                f"Captured {_fmt(m.captured)} ({how}row(s) {_rows_txt(caps)}) fully refunded "
                f"(row(s) {_rows_txt(m.refunds)}) → net {_fmt(m.net)}"
            )
        else:
            category = Category.PARTIALLY_REFUNDED
            explanation = (
                f"Captured {_fmt(m.captured)} ({how}row(s) {_rows_txt(caps)}), refunded "
                f"{_fmt(m.refunded)} (row(s) {_rows_txt(m.refunds)}) → net {_fmt(m.net)}"
            )
        refund_note = ""

    if category not in (
        Category.MATCHED,
        Category.MATCHED_SPLIT,
        Category.REFUNDED,
        Category.PARTIALLY_REFUNDED,
    ):
        unrec[order.currency] += m.unreconciled_vs(order.amount)
        if not typed:
            # Un-netted negative rows are unexplained money on their own.
            unrec[order.currency] += sum(
                (abs(c.amount) for c in caps if c.amount is not None and c.amount < 0), ZERO
            )
    return _group_row(
        category, explanation + refund_note + void_note, order, group, m, duplicates=duplicates
    )


def _orphan_row(group: list[Record], typed: bool, unrec: dict[str, Decimal]) -> ResultRow:
    currencies = sorted({r.currency for r in group})
    for c in currencies:
        # Gross: every captured or refunded amount moved without an order (voids moved none).
        unrec[c] += sum(
            (r.magnitude for r in group if r.currency == c and r.txn_type is not TxnType.VOID),
            ZERO,
        )
    explanation = "No order carries this reference"
    if len(group) > 1:
        explanation += f" ({len(group)} transactions: {_types_txt(group)})"
    money = _GroupMoney(group, typed) if len(currencies) == 1 else None
    if money is None:
        explanation += "; mixed currencies, amounts not summed"
    return _group_row(Category.ORPHAN_PAYMENT, explanation, None, group, money)


def _financials(
    canon_orders: dict[str, Record], payments: list[Record], unrec: dict[str, Decimal]
) -> list[CurrencyTotals]:
    totals: dict[str, CurrencyTotals] = {}

    def get(c: str) -> CurrencyTotals:
        if c not in totals:
            totals[c] = CurrencyTotals(currency=c)
        return totals[c]

    for o in canon_orders.values():
        t = get(o.currency)
        t.orders_count += 1
        t.orders_total += o.amount  # type: ignore[operator]
    for p in payments:
        t = get(p.currency)
        if p.txn_type is TxnType.PAYMENT:
            t.captured += p.amount  # type: ignore[operator]
        elif p.txn_type is TxnType.REFUND:
            t.refunded += p.magnitude
        else:
            t.voided += p.magnitude
    for c, v in unrec.items():
        get(c).unreconciled += v
    return [totals[c] for c in sorted(totals)]


def reconcile_records(
    orders: list[Record],
    payments: list[Record],
    options: ReconcileOptions | None = None,
    *,
    transaction_type_mapped: bool = False,
) -> ReconcileResult:
    options = options or ReconcileOptions()
    typed = transaction_type_mapped
    rows: list[ResultRow] = []
    unrec: dict[str, Decimal] = defaultdict(lambda: ZERO)

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
            row = _row_from_pair(
                None, p, Category.INVALID_ROW, f"Payment row {p.row_number}: {p.error}"
            )
            row.captured_amount = None
            row.transaction_types = p.txn_type_raw.strip() if typed else ""
            rows.append(row)

    canon_orders, dup_orders = _split_orders(valid_orders)
    groups = _payment_groups(valid_payments)

    for first, dup in dup_orders:
        rows.append(
            _row_from_pair(
                dup,
                None,
                Category.DUPLICATE_ORDER,
                f"Reference already seen in order row {first.row_number}",
            )
        )

    unmatched_orders: list[Record] = []
    for ref, order in canon_orders.items():
        group = groups.get(ref)
        if group is None:
            unmatched_orders.append(order)
            continue
        rows.append(_evaluate_group(order, group, options, typed, unrec))

    orphan_groups = [g for ref, g in groups.items() if ref not in canon_orders]

    if options.enable_fallback_matching:
        singles = [g[0] for g in orphan_groups if len(g) == 1 and g[0].txn_type is TxnType.PAYMENT]
        unmatched_orders, left_singles, fallback_rows = _fallback_match(
            unmatched_orders, singles, options.fallback_date_window_days
        )
        rows.extend(fallback_rows)
        paired = {p.row_number for p in singles} - {p.row_number for p in left_singles}
        orphan_groups = [g for g in orphan_groups if g[0].row_number not in paired]

    for order in unmatched_orders:
        assert order.amount is not None
        unrec[order.currency] += abs(order.amount)
        rows.append(
            _row_from_pair(
                order, None, Category.MISSING_PAYMENT, "No payment carries this reference"
            )
        )
    for group in orphan_groups:
        rows.append(_orphan_row(group, typed, unrec))

    rows.sort(key=_sort_key)
    return ReconcileResult(
        rows=rows,
        orders_total=len(orders),
        payments_total=len(payments),
        options=options,
        financials=_financials(canon_orders, valid_payments, unrec),
        transaction_type_mapped=typed,
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
        row = _row_from_pair(
            order,
            chosen,
            Category.FALLBACK_MATCHED,
            f"References differ ({order.reference_raw.strip()} vs "
            f"{chosen.reference_raw.strip()}) but amount, currency and date "
            f"(±{window_days}d) agree — review manually",
        )
        row.net_amount = chosen.amount
        rows.append(row)
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
    # A transaction type only makes sense for the PSP side.
    orders_mapping = ColumnMapping(
        reference=orders_mapping.reference,
        amount=orders_mapping.amount,
        currency=orders_mapping.currency,
        date=orders_mapping.date,
    )
    orders = build_records(
        orders_df, orders_mapping, case_insensitive_reference=options.case_insensitive_reference
    )
    payments = build_records(
        payments_df, payments_mapping, case_insensitive_reference=options.case_insensitive_reference
    )
    return reconcile_records(
        orders,
        payments,
        options,
        transaction_type_mapped=payments_mapping.transaction_type is not None,
    )
