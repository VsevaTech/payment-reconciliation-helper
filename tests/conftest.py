from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from app.models import ColumnMapping, ReconcileOptions
from app.reconcile import reconcile_frames

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "demo-data"
DEMO_FILES = ("orders.csv", "payments.csv", "expected.json")


def _ensure_demo_data() -> None:
    """The demo dataset is derived from ``demo-data/generate.py`` (fixed seed).

    If a checkout lacks the generated files, produce them so the suite is
    self-sufficient; when they are present they are left untouched and
    ``test_generator_is_deterministic`` proves they are in sync.
    """
    if all((DEMO_DIR / name).exists() for name in DEMO_FILES):
        return
    subprocess.run([sys.executable, str(DEMO_DIR / "generate.py")], check=True, capture_output=True)


_ensure_demo_data()

ORDERS_MAP = ColumnMapping(reference="order_id", amount="amount", currency="currency", date="date")
PAYMENTS_MAP = ColumnMapping(
    reference="merchant_reference", amount="amount", currency="currency", date="payment_date"
)


def frame(csv_text: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(csv_text.strip()), dtype=str, keep_default_na=False)


def run(orders_csv: str, payments_csv: str, options: ReconcileOptions | None = None):
    return reconcile_frames(
        frame(orders_csv), frame(payments_csv), ORDERS_MAP, PAYMENTS_MAP, options
    )


@pytest.fixture
def root() -> Path:
    return ROOT


@pytest.fixture
def demo_dir(root: Path) -> Path:
    return root / "demo-data"
