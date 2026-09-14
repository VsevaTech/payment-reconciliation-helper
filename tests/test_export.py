from __future__ import annotations

import csv
import io
from decimal import Decimal

from openpyxl import load_workbook

from app.export import COLUMNS, to_csv, to_xlsx
from tests.conftest import run

ORDERS = "order_id,amount,currency,date\nA-1,100.00,AED,2024-09-01\nA-2,0.10,AED,2024-09-01\n"
PAYMENTS = (
    "merchant_reference,amount,currency,payment_date\n"
    "A-1,100.00,AED,2024-09-01\nA-2,0.30,AED,2024-09-01\n"
)


def test_csv_export_has_bom_header_and_exact_decimals():
    result = run(ORDERS, PAYMENTS)
    data = to_csv(result, include_matched=True)
    assert data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    assert list(rows[0].keys()) == COLUMNS
    mismatch = next(r for r in rows if r["category"] == "AMOUNT_MISMATCH")
    assert mismatch["difference"] == "0.20"
    only_exc = list(csv.DictReader(io.StringIO(to_csv(result, False).decode("utf-8-sig"))))
    assert [r["category"] for r in only_exc] == ["AMOUNT_MISMATCH"]


def test_xlsx_export_sheets_and_numeric_cells():
    result = run(ORDERS, PAYMENTS)
    wb = load_workbook(io.BytesIO(to_xlsx(result)))
    assert wb.sheetnames == ["Summary", "Exceptions", "All rows"]
    summary = {row[0]: row[1] for row in wb["Summary"].iter_rows(min_row=9, values_only=True)}
    assert summary["MATCHED"] == 1 and summary["AMOUNT_MISMATCH"] == 1
    exc = wb["Exceptions"]
    header = [c.value for c in exc[1]]
    assert header == COLUMNS
    row = [c.value for c in exc[2]]
    assert row[0] == "AMOUNT_MISMATCH"
    assert Decimal(str(row[COLUMNS.index("difference")])) == Decimal("0.2")
    assert exc.freeze_panes == "A2"
