"""CSV and XLSX report writers."""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models import CATEGORY_ORDER, Category, ReconcileResult, ResultRow

COLUMNS = [
    "category",
    "reference",
    "order_row",
    "payment_row",
    "order_amount",
    "payment_amount",
    "difference",
    "order_currency",
    "payment_currency",
    "order_date",
    "payment_date",
    "explanation",
]

_FILL = {
    Category.MATCHED: "E6F4EA",
    Category.MISSING_PAYMENT: "FDE7E9",
    Category.ORPHAN_PAYMENT: "FDE7E9",
    Category.AMOUNT_MISMATCH: "FFF4CE",
    Category.CURRENCY_MISMATCH: "FFF4CE",
    Category.DUPLICATE_PAYMENT: "E8E0F7",
    Category.DUPLICATE_ORDER: "E8E0F7",
    Category.FALLBACK_MATCHED: "E0F0FF",
    Category.INVALID_ROW: "EEEEEE",
}


def _rows_for_export(result: ReconcileResult, include_matched: bool) -> list[ResultRow]:
    return result.rows if include_matched else result.exceptions


def to_csv(result: ReconcileResult, include_matched: bool = True) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for r in _rows_for_export(result, include_matched):
        writer.writerow(r.to_dict())
    return buf.getvalue().encode("utf-8-sig")


def to_xlsx(result: ReconcileResult, include_matched: bool = True) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    bold = Font(bold=True)

    ws.append(["Payment Reconciliation Report"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["Orders rows", result.orders_total])
    ws.append(["Payments rows", result.payments_total])
    ws.append(["Amount tolerance", format(result.options.amount_tolerance, "f")])
    ws.append(["Fallback matching", "on" if result.options.enable_fallback_matching else "off"])
    ws.append([])
    ws.append(["Category", "Count"])
    ws["A8"].font = bold
    ws["B8"].font = bold
    summary = result.summary
    for cat in CATEGORY_ORDER:
        ws.append([cat.value, summary[cat.value]])
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 12

    def write_sheet(title: str, rows: list[ResultRow]) -> None:
        sheet = wb.create_sheet(title)
        sheet.append(COLUMNS)
        for cell in sheet[1]:
            cell.font = bold
        for r in rows:
            d = r.to_dict()
            values: list[object] = []
            for col in COLUMNS:
                v = d[col]
                if col in ("order_amount", "payment_amount", "difference") and v != "":
                    # openpyxl writes Decimal as an exact numeric cell.
                    v = Decimal(str(v))
                values.append(v)
            sheet.append(values)
            fill = _FILL.get(r.category)
            if fill:
                sheet.cell(row=sheet.max_row, column=1).fill = PatternFill("solid", fgColor=fill)
        widths = {"category": 20, "reference": 22, "explanation": 70}
        for i, col in enumerate(COLUMNS, start=1):
            sheet.column_dimensions[get_column_letter(i)].width = widths.get(col, 14)
        for i in (5, 6, 7):
            for cell in sheet.iter_cols(min_col=i, max_col=i, min_row=2):
                for c in cell:
                    c.number_format = "#,##0.00"
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

    write_sheet("Exceptions", result.exceptions)
    if include_matched:
        write_sheet("All rows", result.rows)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
