from __future__ import annotations

from decimal import Decimal

from app.models import Category, ReconcileOptions
from tests.conftest import run

ORDERS = """
order_id,amount,currency,date
A-1,100.00,AED,2024-09-01
A-2,250.50,USD,2024-09-02
A-3,19.99,AED,2024-09-03
"""

PAYMENTS = """
merchant_reference,amount,currency,payment_date
A-1,100.00,AED,2024-09-01
A-2,250.50,USD,2024-09-02
A-3,19.99,AED,2024-09-03
"""


def cats(result, category: Category) -> list[str]:
    return [r.reference for r in result.by_category(category)]


def test_perfect_match():
    result = run(ORDERS, PAYMENTS)
    assert result.summary["MATCHED"] == 3
    assert result.exceptions == []
    assert all(r.difference == Decimal("0") for r in result.rows)


def test_missing_payment():
    payments = "merchant_reference,amount,currency,payment_date\nA-1,100.00,AED,2024-09-01\n"
    result = run(ORDERS, payments)
    assert sorted(cats(result, Category.MISSING_PAYMENT)) == ["A-2", "A-3"]
    assert result.summary["MATCHED"] == 1
    row = result.by_category(Category.MISSING_PAYMENT)[0]
    assert row.payment_row is None and row.order_row == 2


def test_orphan_payment():
    payments = PAYMENTS + "Z-9,5.00,AED,2024-09-09\n"
    result = run(ORDERS, payments)
    assert cats(result, Category.ORPHAN_PAYMENT) == ["Z-9"]
    assert result.by_category(Category.ORPHAN_PAYMENT)[0].payment_row == 4


def test_amount_mismatch_with_signed_difference():
    payments = PAYMENTS.replace("A-2,250.50", "A-2,250.40")
    result = run(ORDERS, payments)
    (row,) = result.by_category(Category.AMOUNT_MISMATCH)
    assert row.reference == "A-2"
    assert row.difference == Decimal("-0.10")
    assert "diff -0.10" in row.explanation
    assert result.summary["MATCHED"] == 2


def test_amount_tolerance():
    payments = PAYMENTS.replace("A-2,250.50", "A-2,250.49")
    strict = run(ORDERS, payments)
    lenient = run(ORDERS, payments, ReconcileOptions(amount_tolerance=Decimal("0.01")))
    assert strict.summary["AMOUNT_MISMATCH"] == 1
    assert lenient.summary["AMOUNT_MISMATCH"] == 0 and lenient.summary["MATCHED"] == 3


def test_currency_mismatch_takes_precedence_over_amount():
    payments = PAYMENTS.replace("A-2,250.50,USD", "A-2,919.00,AED")
    result = run(ORDERS, payments)
    assert cats(result, Category.CURRENCY_MISMATCH) == ["A-2"]
    assert result.summary["AMOUNT_MISMATCH"] == 0


def test_currency_is_case_and_whitespace_insensitive():
    payments = PAYMENTS.replace("A-1,100.00,AED", "A-1,100.00, aed ")
    result = run(ORDERS, payments)
    assert result.summary["MATCHED"] == 3


# Repeated references are no longer blanket DUPLICATE_PAYMENT rows: the whole
# payment group is evaluated against the order. A second capture of the full
# amount is a POSSIBLE_DUPLICATE_CAPTURE; the earliest capture (by date, then row)
# is treated as the original and the others are flagged.


def test_duplicate_payment_keeps_first_by_date_as_canonical():
    payments = PAYMENTS + "A-1,100.00,AED,2024-09-05\n"
    result = run(ORDERS, payments)
    (dup,) = result.by_category(Category.POSSIBLE_DUPLICATE_CAPTURE)
    assert dup.reference == "A-1"
    assert dup.payment_rows == [1, 4] and dup.duplicate_payment_rows == [4]
    assert dup.payment_row == 1
    assert "row 1 alone equals the order" in dup.explanation
    assert dup.difference == Decimal("100.00")
    assert result.summary["MATCHED"] == 2


def test_duplicate_payment_earlier_date_later_in_file_becomes_canonical():
    payments = PAYMENTS + "A-1,100.00,AED,2024-08-30\n"
    result = run(ORDERS, payments)
    (dup,) = result.by_category(Category.POSSIBLE_DUPLICATE_CAPTURE)
    assert dup.payment_row == 4
    assert dup.duplicate_payment_rows == [1]


def test_duplicate_with_different_amount_is_still_duplicate_not_mismatch():
    # A second capture with a different amount: one capture alone pays the
    # order, so the extra row is flagged as a possible duplicate, not a mismatch.
    payments = PAYMENTS + "A-1,999.00,AED,2024-09-06\n"
    result = run(ORDERS, payments)
    assert result.summary["POSSIBLE_DUPLICATE_CAPTURE"] == 1
    assert result.summary["AMOUNT_MISMATCH"] == 0
    assert result.by_category(Category.POSSIBLE_DUPLICATE_CAPTURE)[0].duplicate_payment_rows == [4]
    assert result.summary["MATCHED"] == 2


def test_row_order_does_not_matter():
    shuffled_payments = "merchant_reference,amount,currency,payment_date\n" + "\n".join(
        PAYMENTS.strip().splitlines()[1:][::-1]
    )
    shuffled_orders = "order_id,amount,currency,date\n" + "\n".join(
        ORDERS.strip().splitlines()[1:][::-1]
    )
    a = run(ORDERS, PAYMENTS)
    b = run(shuffled_orders, shuffled_payments)
    assert a.summary == b.summary
    assert sorted(r.reference for r in a.rows) == sorted(r.reference for r in b.rows)


def test_reference_whitespace_is_trimmed_but_case_matters_by_default():
    payments = PAYMENTS.replace("A-1,", "  A-1 ,").replace("A-2,", "a-2,")
    result = run(ORDERS, payments)
    assert result.summary["MATCHED"] == 2
    assert cats(result, Category.MISSING_PAYMENT) == ["A-2"]
    assert cats(result, Category.ORPHAN_PAYMENT) == ["a-2"]
    ci = run(ORDERS, payments, ReconcileOptions(case_insensitive_reference=True))
    assert ci.summary["MATCHED"] == 3


def test_invalid_rows_are_reported_not_dropped():
    orders = ORDERS + ",50.00,AED,2024-09-04\nA-9,abc,AED,2024-09-04\n"
    result = run(orders, PAYMENTS)
    invalid = result.by_category(Category.INVALID_ROW)
    assert [r.order_row for r in invalid] == [4, 5]
    assert "empty reference" in invalid[0].explanation
    assert "invalid amount" in invalid[1].explanation
    assert result.summary["MISSING_PAYMENT"] == 0


def test_optional_fallback_matching_is_off_by_default_and_explainable():
    orders = "order_id,amount,currency,date\nA-1,100.00,AED,2024-09-01\n"
    payments = "merchant_reference,amount,currency,payment_date\nPSP-77,100.00,AED,2024-09-02\n"
    strict = run(orders, payments)
    assert strict.summary["MISSING_PAYMENT"] == 1 and strict.summary["ORPHAN_PAYMENT"] == 1

    fb = run(orders, payments, ReconcileOptions(enable_fallback_matching=True))
    assert fb.summary["FALLBACK_MATCHED"] == 1
    assert fb.summary["MISSING_PAYMENT"] == 0 and fb.summary["ORPHAN_PAYMENT"] == 0
    assert "review manually" in fb.by_category(Category.FALLBACK_MATCHED)[0].explanation

    too_far = run(
        orders,
        payments.replace("2024-09-02", "2024-09-20"),
        ReconcileOptions(enable_fallback_matching=True, fallback_date_window_days=3),
    )
    assert too_far.summary["FALLBACK_MATCHED"] == 0


def test_result_rows_are_sorted_by_category_then_row():
    payments = PAYMENTS.replace("A-2,250.50", "A-2,1.00") + "Z-1,1.00,AED,2024-09-01\n"
    orders = ORDERS + "M-1,1.00,AED,2024-09-01\n"
    result = run(orders, payments)
    categories = [r.category for r in result.rows]
    assert categories == sorted(categories, key=list(Category).index)
