"""Domain types shared by the engine, the API and the exporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Category(StrEnum):
    # --- reconciled (the money ties out) -------------------------------------
    MATCHED = "MATCHED"
    # Several captures that together equal the order amount.
    MATCHED_SPLIT = "MATCHED_SPLIT"
    # Captures tie out; some (but not all) of the captured money was refunded.
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    # Captures tie out; everything captured was refunded (net 0).
    REFUNDED = "REFUNDED"
    # --- exceptions ------------------------------------------------------------
    MISSING_PAYMENT = "MISSING_PAYMENT"
    ORPHAN_PAYMENT = "ORPHAN_PAYMENT"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    # Several captures that together are LESS than the order amount.
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    # More captured than ordered, and the surplus looks like a repeated capture.
    POSSIBLE_DUPLICATE_CAPTURE = "POSSIBLE_DUPLICATE_CAPTURE"
    # More refunded than captured (including a refund with no capture at all).
    REFUND_EXCEEDS_CAPTURE = "REFUND_EXCEEDS_CAPTURE"
    # The order only has VOID transactions: nothing was captured.
    VOIDED = "VOIDED"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    # Only produced when the optional fallback matcher is enabled.
    FALLBACK_MATCHED = "FALLBACK_MATCHED"
    # Rows that could not be interpreted (bad amount, empty reference, unknown type).
    INVALID_ROW = "INVALID_ROW"


RECONCILED_CATEGORIES: tuple[Category, ...] = (
    Category.MATCHED,
    Category.MATCHED_SPLIT,
    Category.PARTIALLY_REFUNDED,
    Category.REFUNDED,
)

EXCEPTION_CATEGORIES: tuple[Category, ...] = tuple(
    c for c in Category if c not in RECONCILED_CATEGORIES
)

CATEGORY_ORDER: tuple[Category, ...] = (*RECONCILED_CATEGORIES, *EXCEPTION_CATEGORIES)

# Result-page / API filter groups. Keys are stable identifiers.
FILTER_GROUPS: dict[str, tuple[str, tuple[Category, ...]]] = {
    "matched": ("Matched", (Category.MATCHED,)),
    "split": ("Split", (Category.MATCHED_SPLIT,)),
    "partial": ("Partial", (Category.PARTIAL_PAYMENT,)),
    "refunded": ("Refunded", (Category.PARTIALLY_REFUNDED, Category.REFUNDED)),
    "missing": ("Missing", (Category.MISSING_PAYMENT,)),
    "amount_mismatch": ("Amount mismatch", (Category.AMOUNT_MISMATCH,)),
    "currency_mismatch": ("Currency mismatch", (Category.CURRENCY_MISMATCH,)),
    "duplicates": (
        "Duplicates",
        (Category.POSSIBLE_DUPLICATE_CAPTURE, Category.DUPLICATE_ORDER),
    ),
    "orphans": ("Orphans", (Category.ORPHAN_PAYMENT,)),
    "refund_issues": ("Refund > capture", (Category.REFUND_EXCEEDS_CAPTURE,)),
    "voided": ("Voided", (Category.VOIDED,)),
    "fallback": ("Fallback", (Category.FALLBACK_MATCHED,)),
    "invalid": ("Invalid rows", (Category.INVALID_ROW,)),
}


class TxnType(StrEnum):
    PAYMENT = "PAYMENT"
    REFUND = "REFUND"
    VOID = "VOID"


@dataclass(frozen=True)
class ColumnMapping:
    """Which source column feeds each canonical field."""

    reference: str
    amount: str
    currency: str | None = None
    date: str | None = None
    # Payments only. ``None`` → every row is a PAYMENT (legacy behaviour).
    transaction_type: str | None = None

    def required(self) -> dict[str, str]:
        return {"reference": self.reference, "amount": self.amount}


@dataclass(frozen=True)
class ReconcileOptions:
    amount_tolerance: Decimal = Decimal("0")
    case_insensitive_reference: bool = False
    enable_fallback_matching: bool = False
    fallback_date_window_days: int = 3


@dataclass
class Record:
    """A normalized order or payment row."""

    row_number: int  # 1-based, header excluded — matches what the user sees in Excel
    reference_raw: str
    reference: str
    amount_raw: str
    amount: Decimal | None
    currency_raw: str
    currency: str
    date_raw: str
    date: date | None
    error: str | None = None
    txn_type: TxnType = TxnType.PAYMENT
    txn_type_raw: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def magnitude(self) -> Decimal:
        """Absolute amount; refunds/voids are exported with either sign by PSPs."""
        assert self.amount is not None
        return abs(self.amount)


def _fmt(v: Decimal | None) -> str:
    return "" if v is None else format(v, "f")


def _rows(v: list[int]) -> str:
    return ";".join(str(i) for i in v)


@dataclass
class ResultRow:
    category: Category
    explanation: str
    order_row: int | None = None
    # First contributing payment row (kept for backwards compatibility).
    payment_row: int | None = None
    reference: str = ""
    order_amount: Decimal | None = None
    # Sum of PAYMENT (capture) rows — equals the single payment amount when
    # there is exactly one payment, as before.
    payment_amount: Decimal | None = None
    # captured − ordered (refunds are reported separately, never netted here).
    difference: Decimal | None = None
    order_currency: str = ""
    payment_currency: str = ""
    order_date: str = ""
    payment_date: str = ""
    # Every payment-file row that belongs to this result, ascending.
    payment_rows: list[int] = field(default_factory=list)
    captured_amount: Decimal | None = None
    refunded_amount: Decimal | None = None
    voided_amount: Decimal | None = None
    net_amount: Decimal | None = None
    # e.g. "PAYMENT×2, REFUND"
    transaction_types: str = ""
    # Capture rows that look like repeats (POSSIBLE_DUPLICATE_CAPTURE only).
    duplicate_payment_rows: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "reference": self.reference,
            "order_row": "" if self.order_row is None else self.order_row,
            "payment_row": "" if self.payment_row is None else self.payment_row,
            "payment_rows": _rows(self.payment_rows),
            "order_amount": _fmt(self.order_amount),
            "payment_amount": _fmt(self.payment_amount),
            "captured_amount": _fmt(self.captured_amount),
            "refunded_amount": _fmt(self.refunded_amount),
            "voided_amount": _fmt(self.voided_amount),
            "net_amount": _fmt(self.net_amount),
            "difference": _fmt(self.difference),
            "order_currency": self.order_currency,
            "payment_currency": self.payment_currency,
            "transaction_types": self.transaction_types,
            "duplicate_payment_rows": _rows(self.duplicate_payment_rows),
            "order_date": self.order_date,
            "payment_date": self.payment_date,
            "explanation": self.explanation,
        }


@dataclass
class CurrencyTotals:
    """Money totals for ONE currency. Different currencies are never summed."""

    currency: str
    orders_count: int = 0
    orders_total: Decimal = Decimal("0")
    captured: Decimal = Decimal("0")
    refunded: Decimal = Decimal("0")
    voided: Decimal = Decimal("0")
    unreconciled: Decimal = Decimal("0")

    @property
    def net_captured(self) -> Decimal:
        return self.captured - self.refunded

    def to_dict(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "orders_count": self.orders_count,
            "orders_total": _fmt(self.orders_total),
            "captured": _fmt(self.captured),
            "refunded": _fmt(self.refunded),
            "net_captured": _fmt(self.net_captured),
            "voided": _fmt(self.voided),
            "unreconciled": _fmt(self.unreconciled),
        }


@dataclass
class ReconcileResult:
    rows: list[ResultRow]
    orders_total: int
    payments_total: int
    options: ReconcileOptions
    financials: list[CurrencyTotals] = field(default_factory=list)
    transaction_type_mapped: bool = False

    @property
    def summary(self) -> dict[str, int]:
        counts = dict.fromkeys((c.value for c in CATEGORY_ORDER), 0)
        for r in self.rows:
            counts[r.category.value] += 1
        return counts

    @property
    def exceptions(self) -> list[ResultRow]:
        return [r for r in self.rows if r.category not in RECONCILED_CATEGORIES]

    def by_category(self, category: Category | str) -> list[ResultRow]:
        cat = Category(category)
        return [r for r in self.rows if r.category == cat]

    def by_group(self, group: str) -> list[ResultRow]:
        if group not in FILTER_GROUPS:
            raise ValueError(group)
        cats = FILTER_GROUPS[group][1]
        return [r for r in self.rows if r.category in cats]

    def group_counts(self) -> dict[str, int]:
        s = self.summary
        return {k: sum(s[c.value] for c in cats) for k, (_, cats) in FILTER_GROUPS.items()}

    def financials_dict(self) -> list[dict[str, object]]:
        return [t.to_dict() for t in self.financials]
