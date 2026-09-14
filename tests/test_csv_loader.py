from __future__ import annotations

import pytest

from app.csv_loader import CsvLoadError, load_csv
from app.mapping import guess_mapping


def test_comma_utf8():
    loaded = load_csv(b"a,b\n1,2\n")
    assert loaded.delimiter == "," and loaded.encoding == "utf-8"
    assert loaded.columns == ["a", "b"] and loaded.row_count == 1


def test_semicolon_and_tab_and_pipe():
    assert load_csv(b"a;b;c\n1;2;3\n4;5;6\n").delimiter == ";"
    assert load_csv(b"a\tb\n1\t2\n").delimiter == "\t"
    assert load_csv(b"a|b\n1|2\n").delimiter == "|"


def test_semicolon_with_decimal_commas_is_not_confused():
    loaded = load_csv(b"ref;amount\nA;1,50\nB;2,00\n")
    assert loaded.delimiter == ";"
    assert loaded.df["amount"].tolist() == ["1,50", "2,00"]


def test_utf8_bom_is_stripped_from_header():
    loaded = load_csv("﻿ref,amount\nA,1\n".encode())
    assert loaded.encoding == "utf-8-sig"
    assert loaded.columns == ["ref", "amount"]


def test_cp1252_fallback():
    raw = "ref;name;amount\nA;Café Müller;1.00\n".encode("cp1252")
    loaded = load_csv(raw)
    assert loaded.df["name"].iloc[0] == "Café Müller"
    assert loaded.encoding != "utf-8"


def test_utf16():
    raw = "ref,amount\nA,1.00\n".encode("utf-16")
    loaded = load_csv(raw)
    assert loaded.df["ref"].iloc[0] == "A"


def test_values_stay_strings_and_leading_zeros_survive():
    loaded = load_csv(b"ref,amount\n00042,007.50\n")
    assert loaded.df["ref"].iloc[0] == "00042"
    assert loaded.df["amount"].iloc[0] == "007.50"


def test_crlf_and_quoted_fields():
    loaded = load_csv(b'ref,name,amount\r\n"A-1","Smith, John","1,000.00"\r\n')
    assert loaded.df["name"].iloc[0] == "Smith, John"
    assert loaded.df["amount"].iloc[0] == "1,000.00"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"   \n",
        b"just one column\nvalue\n",
        b"\x00\x01\x02binary\x00\x00",
        b"ref,amount\nA,1\nB,2,3,4\n",  # ragged row
        b"ref,amount\n",  # header only
        b"a,a\n1,2\n",  # duplicate header
    ],
)
def test_invalid_csv_raises(raw: bytes):
    with pytest.raises(CsvLoadError):
        load_csv(raw, filename="bad.csv")


def test_guess_mapping_prefers_exact_names_and_does_not_reuse_columns():
    guess = guess_mapping(["Order ID", "Customer", "Amount", "Currency", "Date", "Channel"])
    assert guess == {
        "reference": "Order ID",
        "amount": "Amount",
        "currency": "Currency",
        "date": "Date",
    }
    psp = guess_mapping(
        ["psp_transaction_id", "merchant_reference", "amount", "currency", "payment_date", "status"]
    )
    assert psp["reference"] == "merchant_reference"
    assert psp["date"] == "payment_date"


def test_guess_mapping_returns_none_when_nothing_fits():
    assert guess_mapping(["foo", "bar"]) == {
        "reference": None,
        "amount": None,
        "currency": None,
        "date": None,
    }
