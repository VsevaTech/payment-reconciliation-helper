"""Command-line entry point: ``python -m app.cli orders.csv payments.csv [options]``."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from app.csv_loader import CsvLoadError, load_csv
from app.export import to_csv, to_xlsx
from app.mapping import guess_mapping
from app.models import CATEGORY_ORDER, ColumnMapping, ReconcileOptions
from app.reconcile import reconcile_frames


def _mapping_from_args(prefix: str, args: argparse.Namespace, columns: list[str]) -> ColumnMapping:
    guess = guess_mapping(columns)
    get = lambda f: getattr(args, f"{prefix}_{f}") or guess.get(f)  # noqa: E731
    ref, amt = get("reference"), get("amount")
    if not ref or not amt:
        sys.exit(f"{prefix}: could not determine reference/amount columns; pass --{prefix}-*")
    return ColumnMapping(reference=ref, amount=amt, currency=get("currency"), date=get("date"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Reconcile orders against PSP payments")
    p.add_argument("orders", type=Path)
    p.add_argument("payments", type=Path)
    for side in ("orders", "payments"):
        for f in ("reference", "amount", "currency", "date"):
            p.add_argument(f"--{side}-{f}", dest=f"{side}_{f}", default=None)
    p.add_argument("--tolerance", default="0")
    p.add_argument("--case-insensitive", action="store_true")
    p.add_argument("--fallback", action="store_true")
    p.add_argument("--window-days", type=int, default=3)
    p.add_argument("--xlsx", type=Path, help="write XLSX report here")
    p.add_argument("--csv", type=Path, help="write CSV report (all rows) here")
    p.add_argument("--json", action="store_true", help="print summary as JSON")
    args = p.parse_args(argv)

    try:
        orders = load_csv(args.orders.read_bytes(), args.orders.name)
        payments = load_csv(args.payments.read_bytes(), args.payments.name)
    except (CsvLoadError, OSError) as exc:
        sys.exit(f"error: {exc}")

    result = reconcile_frames(
        orders.df,
        payments.df,
        _mapping_from_args("orders", args, orders.columns),
        _mapping_from_args("payments", args, payments.columns),
        ReconcileOptions(
            amount_tolerance=Decimal(args.tolerance),
            case_insensitive_reference=args.case_insensitive,
            enable_fallback_matching=args.fallback,
            fallback_date_window_days=args.window_days,
        ),
    )
    summary = result.summary
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        width = max(len(c.value) for c in CATEGORY_ORDER)
        for c in CATEGORY_ORDER:
            print(f"{c.value:<{width}}  {summary[c.value]:>6}")
    if args.xlsx:
        args.xlsx.write_bytes(to_xlsx(result))
    if args.csv:
        args.csv.write_bytes(to_csv(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
