"""The synthetic dataset must reproduce its planted discrepancies exactly."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.csv_loader import load_csv
from app.models import Category, ColumnMapping
from app.reconcile import reconcile_frames


@pytest.fixture(scope="module")
def expected(demo_dir_module: Path) -> dict:
    return json.loads((demo_dir_module / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def demo_dir_module() -> Path:
    return Path(__file__).resolve().parents[1] / "demo-data"


@pytest.fixture(scope="module")
def result(demo_dir_module: Path, expected: dict):
    orders = load_csv((demo_dir_module / "orders.csv").read_bytes(), "orders.csv")
    payments = load_csv((demo_dir_module / "payments.csv").read_bytes(), "payments.csv")
    assert orders.delimiter == "," and payments.delimiter == ";"
    assert orders.encoding == "utf-8" and payments.encoding == "utf-8"
    return reconcile_frames(
        orders.df,
        payments.df,
        ColumnMapping(**expected["orders_mapping"]),
        ColumnMapping(**expected["payments_mapping"]),
    )


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
        ("duplicate_payment", Category.DUPLICATE_PAYMENT),
        ("duplicate_order", Category.DUPLICATE_ORDER),
        ("orphan_payment", Category.ORPHAN_PAYMENT),
    ],
)
def test_planted_references_land_in_their_category(result, expected, planted_key, category):
    got = sorted(r.reference for r in result.by_category(category))
    assert got == expected["planted"][planted_key]


def test_every_input_row_is_accounted_for(result):
    order_rows = sorted(r.order_row for r in result.rows if r.order_row is not None)
    payment_rows = sorted(r.payment_row for r in result.rows if r.payment_row is not None)
    assert order_rows == list(range(1, result.orders_total + 1))
    assert payment_rows == list(range(1, result.payments_total + 1))


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
