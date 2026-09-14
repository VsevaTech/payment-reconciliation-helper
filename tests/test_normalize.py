from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.normalize import AmountParseError, parse_amount, parse_date


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100", Decimal("100")),
        ("100.00", Decimal("100.00")),
        ("0.1", Decimal("0.1")),
        ("12.5", Decimal("12.50")),  # Decimal equality ignores trailing zeros
        ("1,234.56", Decimal("1234.56")),
        ("1 234,56", Decimal("1234.56")),
        ("1.234,56", Decimal("1234.56")),
        ("1.234.567,89", Decimal("1234567.89")),
        ("1,234", Decimal("1234")),  # comma + 3 digits → thousands separator
        ("12,5", Decimal("12.5")),  # comma + 1-2 digits → decimal comma
        ("-12.00", Decimal("-12.00")),
        ("(12.00)", Decimal("-12.00")),
        ("AED 12.50", Decimal("12.50")),
        ("12.50 USD", Decimal("12.50")),
        ("$12.50", Decimal("12.50")),
        ("€1.299,00", Decimal("1299.00")),
        ("  7  ", Decimal("7")),
        (".5", Decimal("0.5")),
        ("0.10", Decimal("0.1")),
        ("99999999999999.99", Decimal("99999999999999.99")),
    ],
)
def test_parse_amount(raw: str, expected: Decimal):
    assert parse_amount(raw) == expected


def test_parse_amount_is_exact_not_float():
    assert parse_amount("0.1") + parse_amount("0.2") == Decimal("0.3")
    assert parse_amount("1.10") - parse_amount("1.00") == Decimal("0.10")


@pytest.mark.parametrize("raw", ["", "   ", "abc", "12.3.4", "1,2,3.4.5", "N/A", "--5", "1e3"])
def test_parse_amount_rejects_garbage(raw: str):
    with pytest.raises(AmountParseError):
        parse_amount(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-09-01", date(2024, 9, 1)),
        ("2024-09-01T13:45:00", date(2024, 9, 1)),
        ("2024-09-01 13:45:00", date(2024, 9, 1)),
        ("2024-09-01T13:45:00Z", date(2024, 9, 1)),
        ("01.09.2024", date(2024, 9, 1)),
        ("01/09/2024", date(2024, 9, 1)),
        ("", None),
        ("not a date", None),
    ],
)
def test_parse_date(raw: str, expected: date | None):
    assert parse_date(raw) == expected
