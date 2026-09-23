"""FastAPI application: upload → map columns → reconcile → export."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import __version__
from app.csv_loader import CsvLoadError, LoadedCsv, load_csv
from app.export import financials_to_csv, to_csv, to_xlsx
from app.mapping import guess_mapping
from app.models import (
    CATEGORY_ORDER,
    FILTER_GROUPS,
    RECONCILED_CATEGORIES,
    Category,
    ColumnMapping,
    ReconcileOptions,
    ReconcileResult,
    ResultRow,
)
from app.normalize import looks_like_txn_type_column
from app.reconcile import reconcile_frames

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def money(value: Decimal | None, signed: bool = False) -> str:
    """Display formatting only: thousands separators, at least two decimals, no rounding."""
    if value is None:
        return ""
    try:
        if value.as_tuple().exponent > -2:  # type: ignore[operator]
            value = value.quantize(Decimal("0.01"))
    except InvalidOperation:  # beyond context precision: show as-is
        pass
    text = format(value, ",f")
    return f"+{text}" if signed and value > 0 else text


templates.env.filters["money"] = money

SESSION_TTL_SECONDS = 60 * 60
NO_CURRENCY = "-"
MAX_SESSIONS = 200


@dataclass
class Session:
    id: str
    orders: LoadedCsv
    payments: LoadedCsv
    created_at: float = field(default_factory=time.time)
    result: ReconcileResult | None = None
    orders_mapping: ColumnMapping | None = None
    payments_mapping: ColumnMapping | None = None


class SessionStore:
    """In-memory, single-process. Good enough for an MVP; see README limitations."""

    def __init__(self) -> None:
        self._data: dict[str, Session] = {}

    def put(self, session: Session) -> None:
        self._evict()
        self._data[session.id] = session

    def get(self, session_id: str) -> Session:
        s = self._data.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="Session not found or expired")
        return s

    def _evict(self) -> None:
        now = time.time()
        expired = [k for k, v in self._data.items() if now - v.created_at > SESSION_TTL_SECONDS]
        for k in expired:
            del self._data[k]
        while len(self._data) >= MAX_SESSIONS:
            oldest = min(self._data.values(), key=lambda s: s.created_at)
            del self._data[oldest.id]


store = SessionStore()
app = FastAPI(title="Payment Reconciliation Helper", version=__version__)


def _render(request: Request, name: str, **ctx: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"version": __version__, **ctx})


def _parse_options(
    tolerance: str, case_insensitive: bool, fallback: bool, window_days: int
) -> ReconcileOptions:
    try:
        tol = Decimal((tolerance or "0").strip().replace(",", "."))
    except InvalidOperation as exc:
        raise HTTPException(400, f"Invalid tolerance: {tolerance!r}") from exc
    if tol < 0:
        raise HTTPException(400, "Tolerance must be non-negative")
    if window_days < 0 or window_days > 365:
        raise HTTPException(400, "Fallback window must be within 0..365 days")
    return ReconcileOptions(
        amount_tolerance=tol,
        case_insensitive_reference=case_insensitive,
        enable_fallback_matching=fallback,
        fallback_date_window_days=window_days,
    )


def _mapping(prefix: str, form: dict[str, str], columns: list[str]) -> ColumnMapping:
    def col(name: str, required: bool) -> str | None:
        v = (form.get(f"{prefix}_{name}") or "").strip()
        if not v:
            if required:
                raise HTTPException(400, f"{prefix}: column for '{name}' is required")
            return None
        if v not in columns:
            raise HTTPException(400, f"{prefix}: column {v!r} does not exist")
        return v

    return ColumnMapping(
        reference=col("reference", True) or "",
        amount=col("amount", True) or "",
        currency=col("currency", False),
        date=col("date", False),
        # Only the payments form offers this field; orders never carry a type.
        transaction_type=col("transaction_type", False) if prefix == "payments" else None,
    )


async def _read_upload(upload: UploadFile, label: str) -> LoadedCsv:
    raw = await upload.read()
    try:
        return load_csv(raw, filename=upload.filename or label)
    except CsvLoadError as exc:
        raise HTTPException(status_code=400, detail=f"{label}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{label}: cannot decode file ({exc})") from exc


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _render(request, "index.html", error=None)


@app.post("/upload")
async def upload(
    request: Request,
    orders: Annotated[UploadFile, File()],
    payments: Annotated[UploadFile, File()],
) -> Response:
    try:
        orders_csv = await _read_upload(orders, "orders")
        payments_csv = await _read_upload(payments, "payments")
    except HTTPException as exc:
        return _render(request, "index.html", error=exc.detail)
    session = Session(id=uuid.uuid4().hex[:12], orders=orders_csv, payments=payments_csv)
    store.put(session)
    return RedirectResponse(url=f"/s/{session.id}/map", status_code=303)


def _payments_guess(loaded: LoadedCsv) -> dict[str, str | None]:
    """Header guess, but a transaction-type column is only pre-selected when its values fit."""
    guess = guess_mapping(loaded.columns)
    col = guess.get("transaction_type")
    if col and not looks_like_txn_type_column(loaded.df[col].tolist()):
        guess["transaction_type"] = None
    return guess


@app.get("/s/{session_id}/map", response_class=HTMLResponse)
async def map_columns(request: Request, session_id: str) -> HTMLResponse:
    s = store.get(session_id)
    return _render(
        request,
        "map.html",
        session=s,
        orders_guess=guess_mapping(s.orders.columns),
        payments_guess=_payments_guess(s.payments),
        error=None,
    )


@app.post("/s/{session_id}/reconcile")
async def run_reconcile(request: Request, session_id: str) -> Response:
    s = store.get(session_id)
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        orders_mapping = _mapping("orders", form, s.orders.columns)
        payments_mapping = _mapping("payments", form, s.payments.columns)
        options = _parse_options(
            form.get("tolerance", "0"),
            form.get("case_insensitive") == "on",
            form.get("fallback") == "on",
            int(form.get("window_days") or 3),
        )
        result = reconcile_frames(
            s.orders.df, s.payments.df, orders_mapping, payments_mapping, options
        )
    except HTTPException as exc:
        return _render(
            request,
            "map.html",
            session=s,
            orders_guess=guess_mapping(s.orders.columns),
            payments_guess=_payments_guess(s.payments),
            error=exc.detail,
        )
    s.result = result
    s.orders_mapping = orders_mapping
    s.payments_mapping = payments_mapping
    return RedirectResponse(url=f"/s/{session_id}/result", status_code=303)


def _result_or_404(s: Session) -> ReconcileResult:
    if s.result is None:
        raise HTTPException(404, "Reconciliation has not been run for this session")
    return s.result


def _select_rows(
    result: ReconcileResult, category: str, group: str, currency: str
) -> tuple[list[ResultRow], str]:
    """Rows for the result table and a human title. ``category`` wins over ``group``."""
    if category:
        try:
            rows, title = result.by_category(category), category
        except ValueError as exc:
            raise HTTPException(400, f"Unknown category {category!r}") from exc
    elif group == "all":
        rows, title = result.rows, "All rows"
    elif group:
        try:
            rows, title = result.by_group(group), FILTER_GROUPS[group][0]
        except ValueError as exc:
            raise HTTPException(400, f"Unknown filter {group!r}") from exc
    else:
        rows, title = result.exceptions, "Exceptions"
    if currency:
        # "-" selects rows whose currency cell was empty.
        want = "" if currency.strip() == NO_CURRENCY else currency.strip().upper()
        rows = [
            r
            for r in rows
            if r.order_currency == want
            or want in [c.strip().replace("∅", "") for c in r.payment_currency.split(",")]
        ]
        title += f" · {want or 'no currency'}"
    return rows, title


@app.get("/s/{session_id}/result", response_class=HTMLResponse)
async def show_result(
    request: Request,
    session_id: str,
    category: str = "",
    filter: str = "",  # noqa: A002 - public query parameter name
    currency: str = "",
) -> HTMLResponse:
    s = store.get(session_id)
    result = _result_or_404(s)
    rows, title = _select_rows(result, category, filter, currency)
    group_counts = result.group_counts()
    ctx = {
        "session": s,
        "result": result,
        "summary": result.summary,
        "categories": [c.value for c in CATEGORY_ORDER],
        "reconciled_categories": [c.value for c in RECONCILED_CATEGORIES],
        "filters": [(k, label, group_counts[k]) for k, (label, _) in FILTER_GROUPS.items()],
        "rows": rows[:2000],
        "rows_total": len(rows),
        "table_title": title,
        "active_category": category,
        "active_filter": filter,
        "active_currency": currency.strip().upper(),
        "currencies": [t.currency or NO_CURRENCY for t in result.financials],
        "matched_value": Category.MATCHED.value,
    }
    if request.headers.get("HX-Request"):
        return _render(request, "_results.html", **ctx)
    return _render(request, "result.html", **ctx)


@app.get("/s/{session_id}/export.csv")
async def export_csv(session_id: str, scope: str = "exceptions") -> Response:
    result = _result_or_404(store.get(session_id))
    data = to_csv(result, include_matched=(scope == "all"))
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="reconciliation.csv"'},
    )


@app.get("/s/{session_id}/export-financials.csv")
async def export_financials_csv(session_id: str) -> Response:
    result = _result_or_404(store.get(session_id))
    return Response(
        content=financials_to_csv(result),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="financial-summary.csv"'},
    )


@app.get("/s/{session_id}/export.xlsx")
async def export_xlsx(session_id: str, scope: str = "all") -> Response:
    result = _result_or_404(store.get(session_id))
    data = to_xlsx(result, include_matched=(scope == "all"))
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="reconciliation.xlsx"'},
    )


@app.post("/api/reconcile")
async def api_reconcile(
    orders: Annotated[UploadFile, File()],
    payments: Annotated[UploadFile, File()],
    orders_reference: Annotated[str, Form()],
    orders_amount: Annotated[str, Form()],
    payments_reference: Annotated[str, Form()],
    payments_amount: Annotated[str, Form()],
    orders_currency: Annotated[str, Form()] = "",
    orders_date: Annotated[str, Form()] = "",
    payments_currency: Annotated[str, Form()] = "",
    payments_date: Annotated[str, Form()] = "",
    payments_transaction_type: Annotated[str, Form()] = "",
    tolerance: Annotated[str, Form()] = "0",
    case_insensitive: Annotated[bool, Form()] = False,
    fallback: Annotated[bool, Form()] = False,
    window_days: Annotated[int, Form()] = 3,
    include_rows: Annotated[bool, Form()] = True,
) -> JSONResponse:
    """Headless variant of the UI flow. Returns summary and (optionally) all rows."""
    orders_csv = await _read_upload(orders, "orders")
    payments_csv = await _read_upload(payments, "payments")
    form = {
        "orders_reference": orders_reference,
        "orders_amount": orders_amount,
        "orders_currency": orders_currency,
        "orders_date": orders_date,
        "payments_reference": payments_reference,
        "payments_amount": payments_amount,
        "payments_currency": payments_currency,
        "payments_date": payments_date,
        "payments_transaction_type": payments_transaction_type,
    }
    om = _mapping("orders", form, orders_csv.columns)
    pm = _mapping("payments", form, payments_csv.columns)
    options = _parse_options(tolerance, case_insensitive, fallback, window_days)
    result = reconcile_frames(orders_csv.df, payments_csv.df, om, pm, options)
    payload: dict[str, object] = {
        "summary": result.summary,
        "financials": result.financials_dict(),
        "transaction_type_mapped": result.transaction_type_mapped,
        "orders_total": result.orders_total,
        "payments_total": result.payments_total,
        "detected": {
            "orders": {"encoding": orders_csv.encoding, "delimiter": orders_csv.delimiter},
            "payments": {"encoding": payments_csv.encoding, "delimiter": payments_csv.delimiter},
        },
    }
    if include_rows:
        payload["rows"] = [r.to_dict() for r in result.rows]
    return JSONResponse(payload)
