"""Command-line entry point: ``python -m app.cli orders.csv payments.csv [options]``."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

from app.csv_loader import CsvLoadError, load_csv
from app.export import to_csv, to_xlsx
from app.mapping import guess_mapping
from app.models import CATEGORY_ORDER, ColumnMapping, ReconcileOptions
from app.normalize import looks_like_txn_type_column
from app.reconcile import reconcile_frames


def _mapping_from_args(
    prefix: str, args: argparse.Namespace, columns: list[str], df: pd.DataFrame | None = None
) -> ColumnMapping:
    guess = guess_mapping(columns)
    get = lambda f: getattr(args, f"{prefix}_{f}", None) or guess.get(f)  # noqa: E731
    ref, amt = get("reference"), get("amount")
    if not ref or not amt:
        sys.exit(f"{prefix}: could not determine reference/amount columns; pass --{prefix}-*")
    txn_type = None
    if prefix == "payments" and not args.no_transaction_type:
        txn_type = args.payments_transaction_type
        guessed = guess.get("transaction_type")
        if txn_type is None and guessed and df is not None:
            # Only adopt a *guessed* column when every value is a known type,
            # otherwise keep the legacy "every row is a capture" behaviour.
            if looks_like_txn_type_column(df[guessed].tolist()):
                txn_type = guessed
            else:
                print(
                    f"note: column {guessed!r} was not used as transaction type "
                    "(unrecognised values); pass --payments-transaction-type to force it",
                    file=sys.stderr,
                )
    return ColumnMapping(
        reference=ref,
        amount=amt,
        currency=get("currency"),
        date=get("date"),
        transaction_type=txn_type,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Reconcile orders against PSP payments")
    p.add_argument("orders", type=Path)
    p.add_argument("payments", type=Path)
    for side in ("orders", "payments"):
        for f in ("reference", "amount", "currency", "date"):
            p.add_argument(f"--{side}-{f}", dest=f"{side}_{f}", default=None)
    p.add_argument(
        "--payments-transaction-type",
        dest="payments_transaction_type",
        default=None,
        help=(
            "payments column with PAYMENT/REFUND/VOID/CHARGEBACK/CHARGEBACK_REVERSAL "
            "(guessed from the header if omitted)"
        ),
    )
    p.add_argument(
        "--no-transaction-type",
        action="store_true",
        help="ignore any transaction-type column: every payment row is a capture (legacy mode)",
    )
    p.add_argument("--tolerance", default="0")
    p.add_argument("--case-insensitive", action="store_true")
    p.add_argument("--fallback", action="store_true")
    p.add_argument("--window-days", type=int, default=3)
    p.add_argument("--xlsx", type=Path, help="write XLSX report here")
    p.add_argument("--csv", type=Path, help="write CSV report (all rows) here")
    p.add_argument("--json", action="store_true", help="print category summary as JSON")
    p.add_argument(
        "--json-full",
        action="store_true",
        help="print summary, per-currency financials and all rows as JSON (API payload shape)",
    )
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
        _mapping_from_args("payments", args, payments.columns, payments.df),
        ReconcileOptions(
            amount_tolerance=Decimal(args.tolerance),
            case_insensitive_reference=args.case_insensitive,
            enable_fallback_matching=args.fallback,
            fallback_date_window_days=args.window_days,
        ),
    )
    summary = result.summary
    if args.json_full:
        payload = {
            "summary": summary,
            "financials": result.financials_dict(),
            "transaction_type_mapped": result.transaction_type_mapped,
            "orders_total": result.orders_total,
            "payments_total": result.payments_total,
            "rows": [r.to_dict() for r in result.rows],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif args.json:
        print(json.dumps(summary, indent=2))
    else:
        width = max(len(c.value) for c in CATEGORY_ORDER)
        for c in CATEGORY_ORDER:
            print(f"{c.value:<{width}}  {summary[c.value]:>6}")
        print()
        cols = [
            "currency",
            "orders_total",
            "captured",
            "refunded",
            "charged_back",
            "chargeback_reversed",
            "net_captured",
            "unreconciled",
        ]
        print("  ".join(f"{c:>14}" for c in cols))
        for t in result.financials_dict():
            print("  ".join(f"{str(t[c]):>14}" for c in cols))
        if not result.transaction_type_mapped:
            print("(transaction type not mapped: refunds/voids/chargebacks are not identified)")
    if args.xlsx:
        args.xlsx.write_bytes(to_xlsx(result))
    if args.csv:
        args.csv.write_bytes(to_csv(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
