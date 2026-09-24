"""Turn raw string cells into typed, comparable values.

All rules here are deterministic and documented in README ("Normalization").
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pandas as pd

from app.models import ColumnMapping, Record, TxnType

_CURRENCY_PREFIX = re.compile(r"^[A-Za-z]{3}\s*|[A-Za-z]{3}$|[€$£₽]")
_SPACES = re.compile(r"[\s  ']")  # space, NBSP, narrow NBSP


class AmountParseError(ValueError):
    pass


MAX_INTEGER_DIGITS = 15
MAX_DECIMALS = 4


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
    # Keeps every sum exact under Decimal's default 28-digit context.
    integer_digits = len(s.split(".")[0].lstrip("0"))
    decimals = len(s.split(".")[1]) if "." in s else 0
    if integer_digits > MAX_INTEGER_DIGITS or decimals > MAX_DECIMALS:
        raise AmountParseError(
            f"out of range (max {MAX_INTEGER_DIGITS} integer digits, "
            f"{MAX_DECIMALS} decimals): {raw!r}"
        )
    return -value if negative else value


_TXN_SYNONYMS: dict[str, TxnType] = {
    **dict.fromkeys(
        ("PAYMENT", "CAPTURE", "CAPTURED", "SALE", "PURCHASE", "CHARGE", "DEBIT"),
        TxnType.PAYMENT,
    ),
    **dict.fromkeys(
        ("REFUND", "REFUNDED", "PARTIAL_REFUND", "CREDIT", "RETURN"),
        TxnType.REFUND,
    ),
    **dict.fromkeys(
        (
            "VOID",
            "VOIDED",
            "CANCEL",
            "CANCELLED",
            "CANCELED",
            "REVERSAL",
            "REVERSED",
            "AUTH_REVERSAL",
        ),
        TxnType.VOID,
    ),
    **dict.fromkeys(
        (
            "CHARGEBACK",
            "CHARGE_BACK",
            "CHARGED_BACK",
            "SECOND_CHARGEBACK",
            "DISPUTE",
            "DISPUTE_LOST",
        ),
        TxnType.CHARGEBACK,
    ),
    **dict.fromkeys(
        (
            "CHARGEBACK_REVERSAL",
            "CHARGEBACK_REVERSED",
            "CHARGE_BACK_REVERSAL",
            "REVERSED_CHARGEBACK",
            "DISPUTE_REVERSAL",
            "DISPUTE_WON",
        ),
        TxnType.CHARGEBACK_REVERSAL,
    ),
}

# Dispute lifecycle events that move no money. Rejected with a precise hint
# instead of being mistaken for a chargeback.
_NO_MONEY_DISPUTE_EVENTS = frozenset(
    {
        "NOTIFICATION_OF_CHARGEBACK",
        "CHARGEBACK_NOTIFICATION",
        "RETRIEVAL_REQUEST",
        "REQUEST_FOR_INFORMATION",
        "DISPUTE_INQUIRY",
        "INQUIRY",
        "DISPUTE_OPENED",
    }
)


class TxnTypeParseError(ValueError):
    pass


def parse_txn_type(raw: str) -> TxnType:
    """Map a PSP transaction-type label onto a :class:`TxnType`.

    Case, surrounding spaces, ``-`` and inner spaces are ignored
    (``"Partial refund"`` → ``PARTIAL_REFUND`` → REFUND). Anything not in the
    documented synonym list is rejected rather than guessed — e.g. ``AUTH``,
    ``FEE`` or ``FAILED`` rows make the row ``INVALID_ROW``. Dispute
    notifications that move no money (``RETRIEVAL_REQUEST`` …) are rejected
    with an explicit reason.
    """
    key = re.sub(r"[\s\-]+", "_", (raw or "").strip().upper())
    if not key:
        raise TxnTypeParseError("empty transaction type")
    if key in _NO_MONEY_DISPUTE_EVENTS:
        raise TxnTypeParseError(
            f"dispute notification {raw.strip()!r} moves no money — filter it out"
        )
    try:
        return _TXN_SYNONYMS[key]
    except KeyError:
        raise TxnTypeParseError(f"unknown transaction type {raw.strip()!r}") from None


def looks_like_txn_type_column(values: list[str]) -> bool:
    """True when every non-empty value is a known transaction type.

    Used only to decide whether a *guessed* column should be pre-selected; an
    explicitly mapped column is always used and bad values become INVALID_ROW.
    """
    seen = False
    for v in values:
        if not str(v).strip():
            continue
        seen = True
        try:
            parse_txn_type(str(v))
        except TxnTypeParseError:
            return False
    return seen


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
    if mapping.transaction_type and mapping.transaction_type not in df.columns:
        missing.append(mapping.transaction_type)
    if missing:
        raise KeyError(f"Mapped columns not found in file: {missing}")

    records: list[Record] = []
    ref_col = df[mapping.reference].tolist()
    amt_col = df[mapping.amount].tolist()
    cur_col = df[mapping.currency].tolist() if mapping.currency else [""] * len(df)
    date_col = df[mapping.date].tolist() if mapping.date else [""] * len(df)
    type_col = df[mapping.transaction_type].tolist() if mapping.transaction_type else [""] * len(df)

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
        type_raw = str(type_col[i])
        txn_type = TxnType.PAYMENT
        if mapping.transaction_type:
            try:
                txn_type = parse_txn_type(type_raw)
            except TxnTypeParseError as exc:
                error = error or str(exc)
            else:
                if txn_type is TxnType.PAYMENT and amount is not None and amount < 0:
                    error = error or (
                        "negative amount on a PAYMENT row (refunds must be typed REFUND)"
                    )
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
                txn_type=txn_type,
                txn_type_raw=type_raw,
            )
        )
    return records
