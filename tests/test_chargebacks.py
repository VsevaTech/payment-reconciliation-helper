"""Chargebacks, chargeback reversals and their interaction with captures and refunds."""

from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest

from app.export import COLUMNS, FINANCIAL_COLUMNS, financials_to_csv, to_csv
from app.models import EXCEPTION_CATEGORIES, RECONCILED_CATEGORIES, Category, ColumnMapping
from app.normalize import TxnTypeParseError, parse_txn_type
from app.reconcile import reconcile_frames
from tests.conftest import ORDERS_MAP, frame, run

TYPED_MAP = ColumnMapping(
    reference="merchant_reference",
    amount="amount",
    currency="currency",
    date="payment_date",
    transaction_type="type",
)
ORDER_100 = "order_id,amount,currency,date\nA-1,100.00,AED,2024-09-01\n"
HDR = "merchant_reference,amount,currency,payment_date,type"
HDR_UNTYPED = "merchant_reference,amount,currency,payment_date"


def typed(orders: str, payments: str):
    return reconcile_frames(frame(orders), frame(payments), ORDERS_MAP, TYPED_MAP)


def pay(*lines: str) -> str:
    return HDR + "\n" + "\n".join(lines) + "\n"


def only(result):
    (row,) = result.rows
    return row


def aed(result):
    (t,) = [t for t in result.financials if t.currency == "AED"]
    return t


D = Decimal


# --- transaction-type parsing -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CHARGEBACK", "CHARGEBACK"),
        ("Charge back", "CHARGEBACK"),
        ("dispute", "CHARGEBACK"),
        ("Second chargeback", "CHARGEBACK"),
        ("chargeback-reversal", "CHARGEBACK_REVERSAL"),
        ("Chargeback Reversed", "CHARGEBACK_REVERSAL"),
        ("DISPUTE_WON", "CHARGEBACK_REVERSAL"),
        # plain REVERSAL stays a VOID (authorisation reversal), as in 0.2
        ("REVERSAL", "VOID"),
    ],
)
def test_parse_chargeback_synonyms(raw, expected):
    assert parse_txn_type(raw).value == expected


@pytest.mark.parametrize("raw", ["Notification of chargeback", "RETRIEVAL_REQUEST", "inquiry"])
def test_dispute_notifications_without_money_are_rejected_explicitly(raw):
    with pytest.raises(TxnTypeParseError, match="moves no money"):
        parse_txn_type(raw)


def test_notification_row_is_invalid_and_does_not_touch_the_order():
    result = typed(
        ORDER_100,
        pay(
            "A-1,100.00,AED,2024-09-01,CAPTURE",
            "A-1,100.00,AED,2024-09-20,NOTIFICATION_OF_CHARGEBACK",
        ),
    )
    assert result.summary["MATCHED"] == 1 and result.summary["INVALID_ROW"] == 1
    (inv,) = result.by_category(Category.INVALID_ROW)
    assert "moves no money" in inv.explanation and inv.payment_rows == [2]


# --- categories -----------------------------------------------------------------


def test_category_sets():
    assert Category.CHARGEBACK_REVERSED in RECONCILED_CATEGORIES
    for c in (
        Category.CHARGED_BACK,
        Category.CHARGEBACK_EXCEEDS_CAPTURE,
        Category.REVERSAL_EXCEEDS_CHARGEBACK,
    ):
        assert c in EXCEPTION_CATEGORIES


def test_full_chargeback_is_charged_back_with_net_zero():
    r = only(
        typed(
            ORDER_100,
            pay("A-1,100.00,AED,2024-09-01,CAPTURE", "A-1,-100.00,AED,2024-10-01,CHARGEBACK"),
        )
    )
    assert r.category is Category.CHARGED_BACK
    assert (r.captured_amount, r.chargeback_amount, r.chargeback_reversed_amount) == (
        D("100.00"),
        D("100.00"),
        D("0"),
    )
    assert r.net_amount == D("0.00") and r.difference == D("0.00")
    assert r.transaction_types == "PAYMENT, CHARGEBACK"
    assert r.payment_rows == [1, 2]
    assert "charged back 100.00 in row(s) 2" in r.explanation


def test_chargeback_sign_does_not_matter():
    neg = only(typed(ORDER_100, pay("A-1,100,AED,,CAPTURE", "A-1,-40,AED,,CHARGEBACK")))
    pos = only(typed(ORDER_100, pay("A-1,100,AED,,CAPTURE", "A-1,40,AED,,CHARGEBACK")))
    assert neg.chargeback_amount == pos.chargeback_amount == D("40")
    assert neg.net_amount == pos.net_amount == D("60")


def test_partial_reversal_leaves_open_chargeback():
    r = only(
        typed(
            ORDER_100,
            pay(
                "A-1,100.00,AED,2024-09-01,CAPTURE",
                "A-1,100.00,AED,2024-10-01,CHARGEBACK",
                "A-1,40.00,AED,2024-11-01,CHARGEBACK_REVERSAL",
            ),
        )
    )
    assert r.category is Category.CHARGED_BACK
    assert r.net_amount == D("40.00")
    assert "60.00 is charged back (partially reversed" in r.explanation


def test_fully_reversed_chargeback_is_reconciled():
    result = typed(
        ORDER_100,
        pay(
            "A-1,60.00,AED,2024-09-01,CAPTURE",
            "A-1,40.00,AED,2024-09-02,CAPTURE",
            "A-1,100.00,AED,2024-10-01,CHARGEBACK",
            "A-1,100.00,AED,2024-11-01,DISPUTE_WON",
        ),
    )
    r = only(result)
    assert r.category is Category.CHARGEBACK_REVERSED
    assert r.net_amount == D("100.00")
    assert "split row(s) 1, 2" in r.explanation and "every chargeback was reversed" in r.explanation
    assert r not in result.exceptions
    assert aed(result).unreconciled == 0


def test_refund_plus_chargeback_within_capture_is_charged_back():
    r = only(
        typed(
            ORDER_100,
            pay(
                "A-1,100.00,AED,2024-09-01,CAPTURE",
                "A-1,30.00,AED,2024-09-05,REFUND",
                "A-1,70.00,AED,2024-10-01,CHARGEBACK",
            ),
        )
    )
    assert r.category is Category.CHARGED_BACK
    assert (r.refunded_amount, r.chargeback_amount, r.net_amount) == (
        D("30.00"),
        D("70.00"),
        D("0.00"),
    )
    assert "refunded 30.00 in row(s) 2; charged back 70.00 in row(s) 3 → net 0.00" in (
        r.explanation
    )


def test_refund_and_chargeback_for_the_same_money_is_flagged():
    result = typed(
        ORDER_100,
        pay(
            "A-1,100.00,AED,2024-09-01,CAPTURE",
            "A-1,100.00,AED,2024-09-03,REFUND",
            "A-1,100.00,AED,2024-10-01,CHARGEBACK",
        ),
    )
    r = only(result)
    assert r.category is Category.CHARGEBACK_EXCEEDS_CAPTURE
    assert r.net_amount == D("-100.00")
    assert "got the money back twice" in r.explanation
    assert aed(result).unreconciled == D("100.00")


def test_reversed_chargeback_does_not_count_against_refunds():
    # refund 100 + chargeback 100 reversed 100 → only the refund is outstanding
    r = only(
        typed(
            ORDER_100,
            pay(
                "A-1,100.00,AED,2024-09-01,CAPTURE",
                "A-1,100.00,AED,2024-09-03,REFUND",
                "A-1,100.00,AED,2024-10-01,CHARGEBACK",
                "A-1,100.00,AED,2024-11-01,CHARGEBACK_REVERSAL",
            ),
        )
    )
    assert r.category is Category.CHARGEBACK_REVERSED
    assert r.net_amount == D("0.00")


def test_chargeback_without_capture():
    result = typed(ORDER_100, pay("A-1,100.00,AED,2024-10-01,CHARGEBACK"))
    r = only(result)
    assert r.category is Category.CHARGEBACK_EXCEEDS_CAPTURE
    assert "nothing was captured" in r.explanation
    # order unpaid (100) + money taken back that was never captured (100)
    assert aed(result).unreconciled == D("200.00")


def test_reversal_without_chargeback():
    result = typed(
        ORDER_100,
        pay("A-1,100.00,AED,2024-09-01,CAPTURE", "A-1,100.00,AED,2024-11-01,CHARGEBACK_REVERSAL"),
    )
    r = only(result)
    assert r.category is Category.REVERSAL_EXCEEDS_CHARGEBACK
    assert r.net_amount == D("200.00")
    assert "nothing was charged back" in r.explanation
    assert aed(result).unreconciled == D("100.00")


def test_reversal_larger_than_chargeback():
    r = only(
        typed(
            ORDER_100,
            pay(
                "A-1,100.00,AED,2024-09-01,CAPTURE",
                "A-1,50.00,AED,2024-10-01,CHARGEBACK",
                "A-1,80.00,AED,2024-11-01,CHARGEBACK_REVERSAL",
            ),
        )
    )
    assert r.category is Category.REVERSAL_EXCEEDS_CHARGEBACK
    assert "30.00 reversed in excess" in r.explanation


def test_capture_problem_wins_over_chargeback():
    result = typed(
        ORDER_100,
        pay(
            "A-1,60.00,AED,2024-09-01,CAPTURE",
            "A-1,35.00,AED,2024-09-02,CAPTURE",
            "A-1,20.00,AED,2024-10-01,CHARGEBACK",
        ),
    )
    r = only(result)
    assert r.category is Category.PARTIAL_PAYMENT
    assert r.difference == D("-5.00") and r.chargeback_amount == D("20.00")
    assert r.net_amount == D("75.00")
    assert "charged back 20.00 in row(s) 3 → net 75.00" in r.explanation
    # the short capture is unreconciled; the chargeback is explained, not unreconciled
    assert aed(result).unreconciled == D("5.00")


def test_orphan_chargeback_counts_gross():
    result = typed(ORDER_100, pay("A-1,100,AED,,CAPTURE", "Z-9,45.00,AED,,CHARGEBACK"))
    (orphan,) = result.by_category(Category.ORPHAN_PAYMENT)
    assert orphan.chargeback_amount == D("45.00") and orphan.net_amount == D("-45.00")
    assert aed(result).unreconciled == D("45.00")


def test_currency_mismatch_with_chargeback_is_not_summed():
    r = only(
        typed(
            ORDER_100,
            pay("A-1,100,AED,,CAPTURE", "A-1,100,USD,,CHARGEBACK"),
        )
    )
    assert r.category is Category.CURRENCY_MISMATCH
    assert r.difference is None and r.chargeback_amount is None


# --- financial summary & filters ------------------------------------------------


def test_financials_include_chargebacks_in_net():
    orders = (
        "order_id,amount,currency,date\n"
        "A-1,100.00,AED,2024-09-01\nA-2,100.00,AED,2024-09-01\nA-3,50.00,AED,2024-09-01\n"
    )
    result = typed(
        orders,
        pay(
            "A-1,100.00,AED,,CAPTURE",
            "A-1,100.00,AED,,CHARGEBACK",  # open
            "A-2,100.00,AED,,CAPTURE",
            "A-2,100.00,AED,,CHARGEBACK",
            "A-2,100.00,AED,,CHARGEBACK_REVERSAL",  # won
            "A-3,50.00,AED,,CAPTURE",
            "A-3,10.00,AED,,REFUND",
        ),
    )
    t = aed(result)
    assert (t.captured, t.refunded, t.charged_back, t.chargeback_reversed) == (
        D("250.00"),
        D("10.00"),
        D("200.00"),
        D("100.00"),
    )
    assert t.net_chargebacks == D("100.00")
    assert t.net_captured == D("140.00")  # 250 − 10 − 200 + 100
    assert t.unreconciled == 0
    d = t.to_dict()
    assert d["charged_back"] == "200.00" and d["chargeback_reversed"] == "100.00"
    assert d["net_captured"] == "140.00"

    groups = result.group_counts()
    assert groups["chargebacks"] == 2
    cats = {r.category for r in result.by_group("chargebacks")}
    assert cats == {Category.CHARGED_BACK, Category.CHARGEBACK_REVERSED}


def test_untyped_mode_does_not_know_chargebacks():
    payments = f"{HDR_UNTYPED}\nA-1,100.00,AED,2024-09-01\nA-1,-100.00,AED,2024-10-01\n"
    result = run(ORDER_100, payments)
    r = only(result)
    assert r.category is Category.AMOUNT_MISMATCH
    assert r.chargeback_amount is None and r.chargeback_reversed_amount is None
    assert aed(result).charged_back == 0


# --- exports ----------------------------------------------------------------------


def test_exports_carry_chargeback_columns():
    result = typed(
        ORDER_100,
        pay("A-1,100.00,AED,,CAPTURE", "A-1,100.00,AED,,CHARGEBACK"),
    )
    rows = list(csv.DictReader(io.StringIO(to_csv(result).decode("utf-8-sig"))))
    assert list(rows[0].keys()) == COLUMNS
    assert rows[0]["category"] == "CHARGED_BACK"
    assert rows[0]["chargeback_amount"] == "100.00"
    assert rows[0]["chargeback_reversed_amount"] == "0"
    fin = list(csv.DictReader(io.StringIO(financials_to_csv(result).decode("utf-8-sig"))))
    assert list(fin[0].keys()) == FINANCIAL_COLUMNS
    assert fin[0]["charged_back"] == "100.00" and fin[0]["net_captured"] == "0.00"
