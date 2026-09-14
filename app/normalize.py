"""Turn raw string cells into typed, comparable values.

All rules here are deterministic and documented in README ("Normalization").
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pandas as pd

from app.models import ColumnMapping, Record

_CURRENCY_PREFIX = re.compile(r"^[A-Za-z]{3}\s*|[A-Za-z]{3}$|[€$£₽]")
_SPACES = re.compile(r"[\s  ']")


class AmountParseError(ValueError):
    pass


def parse_amount(raw: str) -> Decimal:
    """Parse a money string into Decimal without ever touching float.

    Accepted forms: ``1234.56``, ``1,234.56``, ``1 234,56``, ``1.234,56``,
    ``-12.00``, ``(12.00)``, ``AED 12.50``, ``12.50 USD``, ``$12.50``.

    Separator rule: if both ``,`` and ``.`` occur, the right-most one is the
    decimal separator. If only ``,`` occurs it is a decimal separator when
    followed by 1–2 digits at the end, otherwise a thousands separator.
    A lone ``.`` is always a decimal separator.
    """
    if raw is None:
        raise AmountParseError("empty amount")
    s = str(raw).strip()
    if not s:
        raise AmountParseError("empty amount")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    s = _CURRENCY_PREFIX.sub("", s).strip()
    s = _SPACES.sub("", s)
    if s.startswith("-"):
        negative = not negative
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]

    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if re.fullmatch(r"\d+,\d{1,2}", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")

    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s):
        raise AmountParseError(f"not a number: {raw!r}")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:  # pragma: no cover - guarded by regex
        raise AmountParseError(f"not a number: {raw!r}") from exc
    return -value if negative else value


def normalize_currency(raw: str) -> str:
    return (raw or "").strip().upper()


def normalize_reference(raw: str, case_insensitive: bool = False) -> str:
    s = (raw or "").strip()
    return s.upper() if case_insensitive else s


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y",
    "%d.%m.%Y %H:%M:%S",
    "%d/%m/%Y",
    "%d/%m/%Y %H:%M:%S",
    "%Y/%m/%d",
    "%d-%m-%Y",
)


def parse_date(raw: str) -> date | None:
    """Best-effort date parsing; returns ``None`` when unparseable.

    Dates are informational (and used only by the optional fallback matcher),
    so failing to parse a date never invalidates a row.
    """
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        ts = pd.to_datetime(s, dayfirst=True, errors="raise")
    except (ValueError, TypeError):
        return None
    if pd.isna(ts):
        return None
    return ts.date()


def build_records(
    df: pd.DataFrame, mapping: ColumnMapping, *, case_insensitive_reference: bool = False
) -> list[Record]:
    """Convert a string DataFrame into a list of :class:`Record`.

    Rows with an empty reference or an unparseable amount get ``error`` set
    and are reported as ``INVALID_ROW`` by the engine instead of being
    silently dropped.
    """
    missing = [c for c in mapping.required().values() if c not in df.columns]
    if mapping.currency and mapping.currency not in df.columns:
        missing.append(mapping.currency)
    if mapping.date and mapping.date not in df.columns:
        missing.append(mapping.date)
    if missing:
        raise KeyError(f"Mapped columns not found in file: {missing}")

    records: list[Record] = []
    ref_col = df[mapping.reference].tolist()
    amt_col = df[mapping.amount].tolist()
    cur_col = df[mapping.currency].tolist() if mapping.currency else [""] * len(df)
    date_col = df[mapping.date].tolist() if mapping.date else [""] * len(df)

    for i in range(len(df)):
        ref_raw = str(ref_col[i])
        amt_raw = str(amt_col[i])
        cur_raw = str(cur_col[i])
        date_raw = str(date_col[i])
        error = None
        reference = normalize_reference(ref_raw, case_insensitive_reference)
        if not reference:
            error = "empty reference"
        amount: Decimal | None
        try:
            amount = parse_amount(amt_raw)
        except AmountParseError as exc:
            amount = None
            error = error or f"invalid amount ({exc})"
        records.append(
            Record(
                row_number=i + 1,
                reference_raw=ref_raw,
                reference=reference,
                amount_raw=amt_raw,
                amount=amount,
                currency_raw=cur_raw,
                currency=normalize_currency(cur_raw),
                date_raw=date_raw,
                date=parse_date(date_raw),
                error=error,
            )
        )
    return records
