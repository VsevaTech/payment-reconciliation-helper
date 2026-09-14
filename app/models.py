"""Domain types shared by the engine, the API and the exporters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Category(StrEnum):
    MATCHED = "MATCHED"
    MISSING_PAYMENT = "MISSING_PAYMENT"
    ORPHAN_PAYMENT = "ORPHAN_PAYMENT"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    # Only produced when the optional fallback matcher is enabled.
    FALLBACK_MATCHED = "FALLBACK_MATCHED"
    # Rows that could not be interpreted (bad amount, empty reference).
    INVALID_ROW = "INVALID_ROW"


EXCEPTION_CATEGORIES: tuple[Category, ...] = (
    Category.MISSING_PAYMENT,
    Category.ORPHAN_PAYMENT,
    Category.AMOUNT_MISMATCH,
    Category.CURRENCY_MISMATCH,
    Category.DUPLICATE_PAYMENT,
    Category.DUPLICATE_ORDER,
    Category.FALLBACK_MATCHED,
    Category.INVALID_ROW,
)

CATEGORY_ORDER: tuple[Category, ...] = (Category.MATCHED, *EXCEPTION_CATEGORIES)


@dataclass(frozen=True)
class ColumnMapping:
    """Which source column feeds each canonical field."""

    reference: str
    amount: str
    currency: str | None = None
    date: str | None = None

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
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class ResultRow:
    category: Category
    explanation: str
    order_row: int | None = None
    payment_row: int | None = None
    reference: str = ""
    order_amount: Decimal | None = None
    payment_amount: Decimal | None = None
    difference: Decimal | None = None
    order_currency: str = ""
    payment_currency: str = ""
    order_date: str = ""
    payment_date: str = ""

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["category"] = self.category.value
        for k in ("order_amount", "payment_amount", "difference"):
            v = d[k]
            d[k] = "" if v is None else format(v, "f")
        for k in ("order_row", "payment_row"):
            d[k] = "" if d[k] is None else d[k]
        return d


@dataclass
class ReconcileResult:
    rows: list[ResultRow]
    orders_total: int
    payments_total: int
    options: ReconcileOptions

    @property
    def summary(self) -> dict[str, int]:
        counts = dict.fromkeys((c.value for c in CATEGORY_ORDER), 0)
        for r in self.rows:
            counts[r.category.value] += 1
        return counts

    @property
    def exceptions(self) -> list[ResultRow]:
        return [r for r in self.rows if r.category != Category.MATCHED]

    def by_category(self, category: Category | str) -> list[ResultRow]:
        cat = Category(category)
        return [r for r in self.rows if r.category == cat]
