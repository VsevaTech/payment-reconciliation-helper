"""Guess column mapping from header names. Suggestions only — the user confirms."""

from __future__ import annotations

import re

_CANDIDATES: dict[str, tuple[str, ...]] = {
    "reference": (
        "merchant_reference",
        "merchantreference",
        "order_id",
        "orderid",
        "order_ref",
        "order_reference",
        "reference",
        "order_number",
        "ordernumber",
        "order",
        "ref",
        "invoice_id",
        "invoice",
        "merchant_order_id",
        "external_id",
        "tracking_id",
    ),
    "amount": (
        "amount",
        "total",
        "total_amount",
        "gross_amount",
        "captured_amount",
        "sum",
        "value",
        "paid_amount",
        "order_total",
        "price",
    ),
    "currency": ("currency", "currency_code", "ccy", "cur", "iso_currency"),
    "date": (
        "payment_date",
        "date",
        "created_at",
        "created",
        "order_date",
        "paid_at",
        "timestamp",
        "settled_at",
        "transaction_date",
        "booking_date",
        "datetime",
        "time",
    ),
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def guess_mapping(columns: list[str]) -> dict[str, str | None]:
    """Return ``{"reference": col|None, "amount": ..., "currency": ..., "date": ...}``.

    Exact normalized-name matches win, then substring matches. Each source
    column is used at most once. Order of fields matters: reference is claimed
    first so that ``order_date`` is not mistaken for the reference.
    """
    normalized = {c: _norm(c) for c in columns}
    taken: set[str] = set()
    result: dict[str, str | None] = {}

    for field, candidates in _CANDIDATES.items():
        chosen: str | None = None
        for cand in candidates:
            for col, n in normalized.items():
                if col not in taken and n == cand:
                    chosen = col
                    break
            if chosen:
                break
        if chosen is None:
            for cand in candidates:
                for col, n in normalized.items():
                    if col not in taken and len(cand) >= 4 and cand in n:
                        chosen = col
                        break
                if chosen:
                    break
        if chosen:
            taken.add(chosen)
        result[field] = chosen
    return result
