"""The synthetic dataset must reproduce its planted discrepancies exactly."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.csv_loader import load_csv
from app.models import Category, ColumnMapping
from app.reconcile import reconcile_frames

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo-data"))
import verify  # noqa: E402  (demo-data/verify.py — the same checker CI runs)


@pytest.fixture(scope="module")
def expected(demo_dir_module: Path) -> dict:
    return json.loads((demo_dir_module / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def demo_dir_module() -> Path:
    return Path(__file__).resolve().parents[1] / "demo-data"


def _run(demo_dir: Path, expected: dict, *, typed: bool):
    orders = load_csv((demo_dir / "orders.csv").read_bytes(), "orders.csv")
    payments = load_csv((demo_dir / "payments.csv").read_bytes(), "payments.csv")
    assert orders.delimiter == "," and payments.delimiter == ";"
    assert orders.encoding == "utf-8" and payments.encoding == "utf-8"
    pm = dict(expected["payments_mapping"])
    if not typed:
        pm.pop("transaction_type")
    return reconcile_frames(
        orders.df,
        payments.df,
        ColumnMapping(**expected["orders_mapping"]),
        ColumnMapping(**pm),
    )


@pytest.fixture(scope="module")
def result(demo_dir_module: Path, expected: dict):
    return _run(demo_dir_module, expected, typed=True)


@pytest.fixture(scope="module")
def untyped_result(demo_dir_module: Path, expected: dict):
    return _run(demo_dir_module, expected, typed=False)


def _payload(result) -> dict:
    return {
        "summary": result.summary,
        "financials": result.financials_dict(),
        "transaction_type_mapped": result.transaction_type_mapped,
        "rows": [r.to_dict() for r in result.rows],
    }


def test_summary_matches_expected_exactly(result, expected):
    assert result.summary == expected["summary"]
    assert result.orders_total == expected["orders_rows"]
    assert result.payments_total == expected["payments_rows"]


@pytest.mark.parametrize(
    ("planted_key", "category"),
    [
        ("missing_payment", Category.MISSING_PAYMENT),
        ("amount_mismatch", Category.AMOUNT_MISMATCH),
        ("currency_mismatch", Category.CURRENCY_MISMATCH),
        ("possible_duplicate_capture", Category.POSSIBLE_DUPLICATE_CAPTURE),
        ("duplicate_order", Category.DUPLICATE_ORDER),
        ("orphan_payment", Category.ORPHAN_PAYMENT),
        ("matched_split", Category.MATCHED_SPLIT),
        ("partial_payment", Category.PARTIAL_PAYMENT),
        ("partially_refunded", Category.PARTIALLY_REFUNDED),
        ("refunded", Category.REFUNDED),
        ("voided", Category.VOIDED),
    ],
)
def test_planted_references_land_in_their_category(result, expected, planted_key, category):
    got = sorted(r.reference for r in result.by_category(category))
    assert got == expected["planted"][planted_key]


@pytest.mark.parametrize("typed", [True, False])
def test_every_input_row_is_accounted_for(result, untyped_result, typed):
    res = result if typed else untyped_result
    order_rows = sorted(r.order_row for r in res.rows if r.order_row is not None)
    payment_rows = sorted(n for r in res.rows for n in r.payment_rows)
    assert order_rows == list(range(1, res.orders_total + 1))
    assert payment_rows == list(range(1, res.payments_total + 1))


def test_financials_match_independently_planted_totals(result, expected):
    assert result.financials_dict() == expected["financials"]
    currencies = [t["currency"] for t in expected["financials"]]
    assert currencies == sorted(set(currencies)) and len(currencies) > 1


def test_showcase_cases(result, expected):
    by_ref = {r.reference: r for r in result.rows if r.order_row is not None}
    split = by_ref["ORD-2024-01002"]
    assert split.category is Category.MATCHED_SPLIT and len(split.payment_rows) == 2
    refund = by_ref["ORD-2024-01004"]
    assert (refund.captured_amount, refund.refunded_amount, refund.net_amount) == (
        Decimal("100.00"),
        Decimal("30.00"),
        Decimal("70.00"),
    )
    assert by_ref["ORD-2024-01003"].difference == Decimal("-5.00")
    assert by_ref["ORD-2024-01006"].duplicate_payment_rows


def test_shared_verifier_accepts_typed_and_untyped_runs(result, untyped_result, expected):
    assert "per-currency financials" in verify.verify(_payload(result), expected, typed=True)
    verify.verify(_payload(untyped_result), expected, typed=False)


def test_without_transaction_type_refunds_are_not_netted(untyped_result, expected):
    assert untyped_result.summary == expected["summary_without_transaction_type"]
    assert untyped_result.summary["PARTIALLY_REFUNDED"] == 0
    assert all(t["refunded"] == "0" for t in untyped_result.financials_dict())


def test_generator_is_deterministic(demo_dir_module: Path, tmp_path: Path):
    script = (demo_dir_module / "generate.py").read_text(encoding="utf-8")
    (tmp_path / "generate.py").write_text(script, encoding="utf-8")
    subprocess.run([sys.executable, str(tmp_path / "generate.py")], check=True, capture_output=True)
    for name in ("orders.csv", "payments.csv", "expected.json"):
        assert (tmp_path / name).read_bytes() == (demo_dir_module / name).read_bytes(), name


def test_cli_reports_expected_summary(demo_dir_module: Path, expected: dict, tmp_path: Path):
    xlsx = tmp_path / "reconciliation.xlsx"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli",
            str(demo_dir_module / "orders.csv"),
            str(demo_dir_module / "payments.csv"),
            "--json",
            "--xlsx",
            str(xlsx),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=demo_dir_module.parent,
    )
    assert json.loads(proc.stdout) == expected["summary"]
    assert xlsx.stat().st_size > 0
