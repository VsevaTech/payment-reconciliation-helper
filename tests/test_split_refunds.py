"""Split / partial payments, duplicate captures, refunds and voids."""

from __future__ import annotations

import io
from decimal import Decimal

import pandas as pd
import pytest

from app.models import Category, ColumnMapping, ReconcileOptions
from app.normalize import parse_txn_type
from app.reconcile import reconcile_frames
from tests.conftest import ORDERS_MAP, PAYMENTS_MAP, frame, run

TYPED_MAP = ColumnMapping(
    reference="merchant_reference",
    amount="amount",
    currency="currency",
    date="payment_date",
    transaction_type="type",
)

ORDER_100 = "order_id,amount,currency,date\nA-1,100.00,AED,2024-09-01\n"
HDR = "merchant_reference,amount,currency,payment_date"


def typed(orders: str, payments: str, options: ReconcileOptions | None = None):
    return reconcile_frames(frame(orders), frame(payments), ORDERS_MAP, TYPED_MAP, options)


def only(result):
    (row,) = result.rows
    return row


# --- split / partial / duplicate (no transaction type needed) ----------------


def test_split_exact_match_60_plus_40():
    r = only(run(ORDER_100, f"{HDR}\nA-1,60.00,AED,2024-09-01\nA-1,40.00,AED,2024-09-02\n"))
    assert r.category is Category.MATCHED_SPLIT
    assert r.captured_amount == Decimal("100.00") and r.difference == Decimal("0.00")
    assert r.payment_rows == [1, 2]
    assert "60.00 + 40.00" in r.explanation


def test_three_way_split_in_shuffled_file_order():
    payments = (
        f"{HDR}\nA-1,30.00,AED,2024-09-03\nA-1,50.00,AED,2024-09-01\nA-1,20.00,AED,2024-09-02\n"
    )
    r = only(run(ORDER_100, payments))
    assert r.category is Category.MATCHED_SPLIT
    assert r.payment_rows == [1, 2, 3]
    # captures listed chronologically in the explanation, earliest is primary
    assert "rows 2, 3, 1" in r.explanation and r.payment_row == 2


def test_partial_payment_60_plus_35():
    r = only(run(ORDER_100, f"{HDR}\nA-1,60.00,AED,2024-09-01\nA-1,35.00,AED,2024-09-02\n"))
    assert r.category is Category.PARTIAL_PAYMENT
    assert r.difference == Decimal("-5.00")
    assert r.captured_amount == Decimal("95.00")
    assert r.payment_rows == [1, 2]


def test_split_respects_tolerance():
    payments = f"{HDR}\nA-1,60.00,AED,2024-09-01\nA-1,39.99,AED,2024-09-02\n"
    assert only(run(ORDER_100, payments)).category is Category.PARTIAL_PAYMENT
    lenient = run(ORDER_100, payments, ReconcileOptions(amount_tolerance=Decimal("0.01")))
    assert only(lenient).category is Category.MATCHED_SPLIT


def test_duplicate_capture_100_plus_100():
    r = only(run(ORDER_100, f"{HDR}\nA-1,100.00,AED,2024-09-01\nA-1,100.00,AED,2024-09-01\n"))
    assert r.category is Category.POSSIBLE_DUPLICATE_CAPTURE
    assert r.difference == Decimal("100.00")
    assert r.payment_rows == [1, 2] and r.duplicate_payment_rows == [2]


def test_repeated_part_of_a_split_is_a_duplicate():
    payments = (
        f"{HDR}\nA-1,60.00,AED,2024-09-01\nA-1,40.00,AED,2024-09-02\nA-1,40.00,AED,2024-09-02\n"
    )
    r = only(run(ORDER_100, payments))
    assert r.category is Category.POSSIBLE_DUPLICATE_CAPTURE
    assert r.duplicate_payment_rows == [3]


def test_overcapture_without_a_repeat_is_amount_mismatch():
    r = only(run(ORDER_100, f"{HDR}\nA-1,60.00,AED,2024-09-01\nA-1,50.00,AED,2024-09-02\n"))
    assert r.category is Category.AMOUNT_MISMATCH
    assert r.difference == Decimal("10.00")
    assert "no repeated capture" in r.explanation
    assert r.duplicate_payment_rows == []


def test_single_short_payment_stays_amount_mismatch():
    # Backwards compatible: one payment that differs is AMOUNT_MISMATCH, not PARTIAL.
    r = only(run(ORDER_100, f"{HDR}\nA-1,95.00,AED,2024-09-01\n"))
    assert r.category is Category.AMOUNT_MISMATCH and r.difference == Decimal("-5.00")


def test_orphan_group_is_one_row_with_all_source_rows():
    payments = f"{HDR}\nZ-1,10.00,AED,2024-09-01\nZ-1,10.00,AED,2024-09-02\n"
    result = run(ORDER_100, payments)
    (orphan,) = result.by_category(Category.ORPHAN_PAYMENT)
    assert orphan.payment_rows == [1, 2] and orphan.captured_amount == Decimal("20.00")
    assert "2 transactions" in orphan.explanation


# --- refunds / voids (transaction_type mapped) --------------------------------

HDR_T = HDR + ",type"


def test_partial_refund():
    r = only(
        typed(
            ORDER_100,
            f"{HDR_T}\nA-1,100.00,AED,2024-09-01,CAPTURE\nA-1,-30.00,AED,2024-09-05,REFUND\n",
        )
    )
    assert r.category is Category.PARTIALLY_REFUNDED
    assert r.captured_amount == Decimal("100.00")
    assert r.refunded_amount == Decimal("30.00")
    assert r.net_amount == Decimal("70.00")
    assert r.difference == Decimal("0.00")
    assert r.transaction_types == "PAYMENT, REFUND"
    assert r.payment_rows == [1, 2]


def test_refund_sign_does_not_matter():
    a = typed(
        ORDER_100, f"{HDR_T}\nA-1,100.00,AED,2024-09-01,PAYMENT\nA-1,30.00,AED,2024-09-05,REFUND\n"
    )
    b = typed(
        ORDER_100, f"{HDR_T}\nA-1,100.00,AED,2024-09-01,PAYMENT\nA-1,-30.00,AED,2024-09-05,REFUND\n"
    )
    assert only(a).net_amount == only(b).net_amount == Decimal("70.00")


def test_full_refund():
    r = only(
        typed(
            ORDER_100,
            f"{HDR_T}\nA-1,100.00,AED,2024-09-01,PAYMENT\nA-1,100.00,AED,2024-09-05,REFUND\n",
        )
    )
    assert r.category is Category.REFUNDED
    assert r.net_amount == Decimal("0.00") and r.refunded_amount == Decimal("100.00")


def test_split_then_full_refund():
    payments = (
        f"{HDR_T}\nA-1,60.00,AED,2024-09-01,SALE\nA-1,40.00,AED,2024-09-02,SALE\n"
        "A-1,-100.00,AED,2024-09-05,Refunded\n"
    )
    r = only(typed(ORDER_100, payments))
    assert r.category is Category.REFUNDED and r.payment_rows == [1, 2, 3]
    assert "split" in r.explanation


def test_refund_exceeding_capture():
    r = only(
        typed(
            ORDER_100,
            f"{HDR_T}\nA-1,100.00,AED,2024-09-01,PAYMENT\nA-1,130.00,AED,2024-09-05,REFUND\n",
        )
    )
    assert r.category is Category.REFUND_EXCEEDS_CAPTURE
    assert r.net_amount == Decimal("-30.00")


def test_refund_without_capture():
    r = only(typed(ORDER_100, f"{HDR_T}\nA-1,-100.00,AED,2024-09-05,REFUND\n"))
    assert r.category is Category.REFUND_EXCEEDS_CAPTURE
    assert r.captured_amount == Decimal("0")


def test_refund_does_not_hide_a_capture_problem():
    payments = f"{HDR_T}\nA-1,90.00,AED,2024-09-01,PAYMENT\nA-1,-10.00,AED,2024-09-05,REFUND\n"
    r = only(typed(ORDER_100, payments))
    assert r.category is Category.AMOUNT_MISMATCH
    assert r.difference == Decimal("-10.00") and r.refunded_amount == Decimal("10.00")
    assert "refunded 10.00" in r.explanation


def test_void_only_order():
    r = only(typed(ORDER_100, f"{HDR_T}\nA-1,100.00,AED,2024-09-01,VOID\n"))
    assert r.category is Category.VOIDED
    assert r.captured_amount == Decimal("0") and r.voided_amount == Decimal("100.00")
    assert r.net_amount == Decimal("0")


def test_void_next_to_capture_is_not_counted():
    payments = f"{HDR_T}\nA-1,100.00,AED,2024-09-01,VOID\nA-1,100.00,AED,2024-09-01,CAPTURE\n"
    r = only(typed(ORDER_100, payments))
    assert r.category is Category.MATCHED
    assert r.voided_amount == Decimal("100.00") and r.captured_amount == Decimal("100.00")
    assert r.payment_row == 2 and r.payment_rows == [1, 2]
    assert "not counted as captured" in r.explanation


def test_voided_duplicate_capture_is_matched():
    payments = f"{HDR_T}\nA-1,100.00,AED,2024-09-01,CAPTURE\nA-1,100.00,AED,2024-09-01,VOIDED\n"
    assert only(typed(ORDER_100, payments)).category is Category.MATCHED


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PAYMENT", "PAYMENT"),
        (" capture ", "PAYMENT"),
        ("Sale", "PAYMENT"),
        ("refund", "REFUND"),
        ("Partial refund", "REFUND"),
        ("partial-refund", "REFUND"),
        ("VOID", "VOID"),
        ("cancelled", "VOID"),
        ("Auth Reversal", "VOID"),
    ],
)
def test_parse_txn_type_synonyms(raw, expected):
    assert parse_txn_type(raw).value == expected


def test_unknown_or_empty_type_is_invalid_not_guessed():
    payments = (
        f"{HDR_T}\nA-1,100.00,AED,2024-09-01,PAYMENT\nA-1,100.00,AED,2024-09-02,FEE\n"
        "A-1,5.00,AED,2024-09-02,\n"
    )
    result = typed(ORDER_100, payments)
    invalid = result.by_category(Category.INVALID_ROW)
    assert [r.payment_rows for r in invalid] == [[2], [3]]
    assert "unknown transaction type 'FEE'" in invalid[0].explanation
    assert "empty transaction type" in invalid[1].explanation
    assert result.summary["MATCHED"] == 1


def test_negative_payment_row_is_invalid_when_typed():
    result = typed(ORDER_100, f"{HDR_T}\nA-1,-100.00,AED,2024-09-01,PAYMENT\n")
    assert result.summary["INVALID_ROW"] == 1 and result.summary["MISSING_PAYMENT"] == 1


# --- behaviour when transaction_type is NOT mapped ---------------------------


def test_untyped_mode_never_nets_refunds():
    payments = f"{HDR}\nA-1,100.00,AED,2024-09-01\nA-1,-30.00,AED,2024-09-05\n"
    r = only(run(ORDER_100, payments))
    assert r.category is Category.AMOUNT_MISMATCH
    assert "no transaction-type column is mapped" in r.explanation
    assert r.refunded_amount is None and r.voided_amount is None


def test_untyped_mode_ignores_a_type_column_that_is_present():
    payments = f"{HDR_T}\nA-1,100.00,AED,2024-09-01,PAYMENT\nA-1,30.00,AED,2024-09-05,REFUND\n"
    untyped = run(ORDER_100, payments)  # PAYMENTS_MAP has no transaction_type
    # Read as two captures, the refund looks like an extra capture — exactly why
    # the column should be mapped when the export mixes types.
    assert only(untyped).category is Category.POSSIBLE_DUPLICATE_CAPTURE
    assert only(untyped).captured_amount == Decimal("130.00")
    assert untyped.transaction_type_mapped is False
    assert only(typed(ORDER_100, payments)).category is Category.PARTIALLY_REFUNDED


def test_untyped_single_negative_payment_is_legacy_amount_mismatch():
    r = only(run(ORDER_100, f"{HDR}\nA-1,-100.00,AED,2024-09-01\n"))
    assert r.category is Category.AMOUNT_MISMATCH and r.difference == Decimal("-200.00")


def test_untyped_and_typed_agree_when_every_row_is_a_payment():
    payments = f"{HDR_T}\nA-1,60.00,AED,2024-09-01,PAYMENT\nA-1,40.00,AED,2024-09-02,PAYMENT\n"
    a, b = run(ORDER_100, payments), typed(ORDER_100, payments)
    assert a.summary == b.summary
    assert only(a).captured_amount == only(b).captured_amount


# --- currencies and financial summary ------------------------------------------


def test_currencies_are_never_summed():
    orders = "order_id,amount,currency,date\nA-1,100.00,AED,2024-09-01\nB-1,50.00,USD,2024-09-01\n"
    payments = (
        f"{HDR_T}\nA-1,100.00,AED,2024-09-01,PAYMENT\n"
        "B-1,50.00,USD,2024-09-01,PAYMENT\nB-1,-5.00,USD,2024-09-02,REFUND\n"
    )
    result = typed(orders, payments)
    fin = {t["currency"]: t for t in result.financials_dict()}
    assert set(fin) == {"AED", "USD"}
    assert fin["AED"]["orders_total"] == "100.00" and fin["AED"]["captured"] == "100.00"
    assert fin["USD"]["captured"] == "50.00" and fin["USD"]["refunded"] == "5.00"
    assert fin["USD"]["net_captured"] == "45.00"
    assert fin["AED"]["unreconciled"] == fin["USD"]["unreconciled"] == "0"


def test_mixed_currency_group_is_currency_mismatch_without_sums():
    payments = f"{HDR}\nA-1,60.00,AED,2024-09-01\nA-1,40.00,USD,2024-09-02\n"
    result = run(ORDER_100, payments)
    r = only(result)
    assert r.category is Category.CURRENCY_MISMATCH
    assert r.captured_amount is None and r.difference is None
    assert r.payment_currency == "AED, USD"
    assert "not summed across currencies" in r.explanation
    fin = {t["currency"]: t for t in result.financials_dict()}
    assert fin["AED"]["unreconciled"] == "100.00"  # the order side
    assert fin["USD"]["unreconciled"] == "40.00"  # the foreign payment


def test_single_foreign_currency_payment_has_no_cross_currency_difference():
    r = only(run(ORDER_100, f"{HDR}\nA-1,100.00,USD,2024-09-01\n"))
    assert r.category is Category.CURRENCY_MISMATCH and r.difference is None


def test_unreconciled_amounts_per_category():
    orders = (
        "order_id,amount,currency,date\n"
        "M-1,100.00,AED,2024-09-01\n"  # missing → 100
        "P-1,100.00,AED,2024-09-01\n"  # partial 95 → 5
        "D-1,100.00,AED,2024-09-01\n"  # duplicate → 100
        "R-1,100.00,AED,2024-09-01\n"  # partially refunded → 0
        "S-1,100.00,AED,2024-09-01\n"  # split → 0
    )
    payments = (
        f"{HDR_T}\n"
        "P-1,60.00,AED,2024-09-01,PAYMENT\nP-1,35.00,AED,2024-09-01,PAYMENT\n"
        "D-1,100.00,AED,2024-09-01,PAYMENT\nD-1,100.00,AED,2024-09-01,PAYMENT\n"
        "R-1,100.00,AED,2024-09-01,PAYMENT\nR-1,30.00,AED,2024-09-03,REFUND\n"
        "S-1,60.00,AED,2024-09-01,PAYMENT\nS-1,40.00,AED,2024-09-01,PAYMENT\n"
        "O-1,7.50,AED,2024-09-01,PAYMENT\n"  # orphan → 7.50
    )
    (aed,) = typed(orders, payments).financials
    assert aed.orders_total == Decimal("500.00")
    assert aed.captured == Decimal("502.50")  # 95 + 200 + 100 + 100 + 7.50
    assert aed.refunded == Decimal("30.00")
    assert aed.net_captured == Decimal("472.50")
    assert aed.unreconciled == Decimal("212.50")


# --- precision & traceability ----------------------------------------------------


def test_decimal_precision_no_float_drift():
    # 0.1 + 0.2 != 0.3 in float; must match exactly here.
    orders = "order_id,amount,currency,date\nA-1,0.30,AED,2024-09-01\n"
    r = only(run(orders, f"{HDR}\nA-1,0.10,AED,2024-09-01\nA-1,0.20,AED,2024-09-01\n"))
    assert r.category is Category.MATCHED_SPLIT
    assert isinstance(r.captured_amount, Decimal) and r.captured_amount == Decimal("0.30")

    three_dp = "order_id,amount,currency,date\nK-1,10.005,KWD,2024-09-01\n"
    r = only(run(three_dp, f"{HDR}\nK-1,5.002,KWD,2024-09-01\nK-1,5.002,KWD,2024-09-01\n"))
    assert r.category is Category.PARTIAL_PAYMENT and r.difference == Decimal("-0.001")


def test_many_small_splits_sum_exactly():
    orders = "order_id,amount,currency,date\nA-1,1.00,AED,2024-09-01\n"
    payments = HDR + "\n" + "\n".join("A-1,0.01,AED,2024-09-01" for _ in range(100)) + "\n"
    r = only(run(orders, payments))
    assert r.category is Category.MATCHED_SPLIT and r.captured_amount == Decimal("1.00")
    assert r.payment_rows == list(range(1, 101))


def test_every_payment_row_is_traceable_to_exactly_one_result():
    orders = (
        "order_id,amount,currency,date\n"
        "A-1,100,AED,2024-09-01\nB-1,50,AED,2024-09-01\nC-1,10,AED,2024-09-01\n"
    )
    payments = (
        f"{HDR_T}\n"
        "B-1,50,AED,2024-09-01,PAYMENT\n"
        "A-1,60,AED,2024-09-01,PAYMENT\n"
        "Z-9,1,AED,2024-09-01,PAYMENT\n"
        "A-1,40,AED,2024-09-02,PAYMENT\n"
        "B-1,50,AED,2024-09-03,REFUND\n"
        "A-1,1,AED,2024-09-02,BOGUS\n"
        "B-1,50,AED,2024-09-01,VOID\n"
    )
    result = typed(orders, payments)
    rows = sorted(n for r in result.rows for n in r.payment_rows)
    assert rows == list(range(1, 8))
    by_ref = {r.reference: r for r in result.rows if r.category is not Category.INVALID_ROW}
    assert by_ref["A-1"].payment_rows == [2, 4]
    assert by_ref["B-1"].payment_rows == [1, 5, 7] and by_ref["B-1"].category is Category.REFUNDED
    assert by_ref["Z-9"].payment_rows == [3]
    assert by_ref["C-1"].category is Category.MISSING_PAYMENT and by_ref["C-1"].payment_rows == []


def test_result_to_dict_serialises_row_lists_and_amounts_as_strings():
    r = only(
        typed(
            ORDER_100, f"{HDR_T}\nA-1,100.00,AED,2024-09-01,PAYMENT\nA-1,30,AED,2024-09-05,REFUND\n"
        )
    ).to_dict()
    assert r["payment_rows"] == "1;2"
    assert r["net_amount"] == "70.00" and r["refunded_amount"] == "30"
    assert all(isinstance(v, str | int) for v in r.values())


def test_fallback_still_works_with_groups():
    orders = "order_id,amount,currency,date\nA-1,100.00,AED,2024-09-01\n"
    payments = (
        f"{HDR}\nPSP-1,100.00,AED,2024-09-02\n"
        "PSP-2,5.00,AED,2024-09-02\nPSP-2,5.00,AED,2024-09-02\n"
    )
    result = run(orders, payments, ReconcileOptions(enable_fallback_matching=True))
    assert result.summary["FALLBACK_MATCHED"] == 1
    (orphan,) = result.by_category(Category.ORPHAN_PAYMENT)
    assert orphan.payment_rows == [2, 3]


def test_frame_helper_keeps_strings():
    df = pd.read_csv(io.StringIO("a\n007\n"), dtype=str, keep_default_na=False)
    assert frame("a\n007\n").equals(df)


def test_orders_mapping_transaction_type_is_ignored():
    om = ColumnMapping(
        reference="order_id", amount="amount", currency="currency", transaction_type="nope"
    )
    result = reconcile_frames(
        frame(ORDER_100), frame(f"{HDR}\nA-1,100.00,AED,2024-09-01\n"), om, PAYMENTS_MAP
    )
    assert result.summary["MATCHED"] == 1


# --- regressions found in review -------------------------------------------------


def test_duplicated_split_is_a_possible_duplicate_capture():
    payments = (
        f"{HDR}\nA-1,50.00,AED,2024-09-01\nA-1,50.00,AED,2024-09-01\nA-1,50.00,AED,2024-09-02\n"
    )
    r = only(run(ORDER_100, payments))
    assert r.category is Category.POSSIBLE_DUPLICATE_CAPTURE
    assert r.duplicate_payment_rows == [3]


def test_unreconciled_is_never_negative_for_negative_orders():
    orders = "order_id,amount,currency,date\nB,-50,USD,2024-09-01\nC,-20,USD,2024-09-01\n"
    result = run(orders, f"{HDR}\nB,-50,EUR,2024-09-01\n")
    fin = {t.currency: t for t in result.financials}
    assert fin["USD"].unreconciled == Decimal("70")
    assert fin["EUR"].unreconciled == Decimal("50")


def test_orphan_unreconciled_is_gross_not_netted():
    result = run(
        ORDER_100, f"{HDR}\nA-1,100,AED,2024-09-01\nZ,100,AED,2024-09-01\nZ,-100,AED,2024-09-02\n"
    )
    (aed,) = result.financials
    assert aed.unreconciled == Decimal("200")


def test_untyped_negative_rows_count_as_unreconciled():
    result = run(ORDER_100, f"{HDR}\nA-1,150,AED,2024-09-01\nA-1,-50,AED,2024-09-02\n")
    assert only(result).category is Category.AMOUNT_MISMATCH
    assert result.financials[0].unreconciled == Decimal("50")


@pytest.mark.parametrize("raw", ["1234567890123456", "1.12345", "99999999999999999999999999999"])
def test_out_of_range_amounts_are_invalid_rows(raw):
    result = run(ORDER_100, f"{HDR}\nA-1,{raw},AED,2024-09-01\n")
    assert result.summary["INVALID_ROW"] == 1
    assert "out of range" in result.by_category(Category.INVALID_ROW)[0].explanation


def test_guess_helper_accepts_only_known_types():
    from app.normalize import looks_like_txn_type_column

    assert looks_like_txn_type_column(["CAPTURE", "refund", "", "void"])
    assert not looks_like_txn_type_column(["card", "CAPTURE"])
    assert not looks_like_txn_type_column(["", ""])
