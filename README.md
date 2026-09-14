# Payment Reconciliation Helper

[![CI](https://github.com/VsevaTech/payment-reconciliation-helper/actions/workflows/ci.yml/badge.svg)](https://github.com/VsevaTech/payment-reconciliation-helper/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![license](https://img.shields.io/badge/license-MIT-green)

Upload an internal **orders** export and a PSP / acquirer **payments** export, map four columns,
and get a deterministic list of discrepancies — missing payments, orphan captures, amount and
currency mismatches, duplicates — with an XLSX/CSV report you can hand to finance.

```
orders.csv + payments.csv → map columns → reconcile → matches & exceptions → export
```

```
MATCHED              989
MISSING_PAYMENT        6
ORPHAN_PAYMENT         5
AMOUNT_MISMATCH        3
CURRENCY_MISMATCH      2
DUPLICATE_PAYMENT      2
DUPLICATE_ORDER        2
```

## Why this exists

Every merchant that accepts card payments through a PSP (Checkout.com, Adyen, Stripe, Network
International, Telr, …) ends up with two sources of truth that should agree but never quite do:

- the **order ledger** in the shop / ERP / POS backend — what the business thinks it sold;
- the **settlement or transaction export** from the PSP or acquiring bank — what was actually
  captured and will be paid out.

Finance has to prove, for each settlement period, that every order was paid once for the right
amount in the right currency, and that every captured payment belongs to a real order. In practice
this is done in Excel with `VLOOKUP`, once a week, by one person who knows where the bodies are
buried. Typical findings:

| Finding | Real-world cause |
|---|---|
| Order without payment | Customer abandoned 3-DS, webhook lost, capture failed after authorisation |
| Payment without order | Order deleted/re-created after payment, reference typo in a payment link, test transactions in prod |
| Amount mismatch | Partial capture, PSP fee deducted before export, rounding in FX conversion, coupon applied post-checkout |
| Currency mismatch | Dynamic Currency Conversion at the terminal, multi-currency pricing bug |
| Duplicate payment | Double-click on "Pay", retry storm on a timeout, manual capture after auto-capture |
| Duplicate order | ERP export joins one order per line item, or a replayed integration message |

This tool automates exactly that check. It is intentionally small: exact matching, explicit
rules, no machine learning, no database — a reviewer must be able to explain every line in the
report.

## Quick start

### Docker

```bash
docker compose up --build
# → http://localhost:8000
```

### Local

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

### Demo (2 minutes)

1. Open <http://localhost:8000>.
2. Upload `demo-data/orders.csv` (UTF-8, comma) and `demo-data/payments.csv` (semicolon, CRLF,
   shuffled). Delimiter and encoding are detected automatically; a preview is shown.
3. The mapping page pre-selects `Order ID → merchant_reference`, `Amount → amount`,
   `Currency → currency`, `Date → payment_date`. Confirm.
4. **Run reconciliation** → the summary tiles show the planted discrepancies listed above
   (see [`demo-data/expected.json`](demo-data/expected.json)). Click a tile to filter the table.
5. **Export XLSX** → `reconciliation.xlsx` with `Summary`, `Exceptions` and `All rows` sheets.

The same flow without a browser:

```bash
python -m app.cli demo-data/orders.csv demo-data/payments.csv --xlsx reconciliation.xlsx
```

or over HTTP:

```bash
curl -s http://localhost:8000/api/reconcile \
  -F orders=@demo-data/orders.csv -F payments=@demo-data/payments.csv \
  -F orders_reference="Order ID" -F orders_amount=Amount -F orders_currency=Currency -F orders_date=Date \
  -F payments_reference=merchant_reference -F payments_amount=amount \
  -F payments_currency=currency -F payments_date=payment_date -F include_rows=false | jq .summary
```

## Matching rules

All rules are deterministic, run in this order, and each result row carries a one-line
`explanation` plus the 1-based source row numbers so it can be checked against the original files.

1. **Normalisation**
   - reference: trimmed; case-sensitive unless *case-insensitive* is ticked;
   - amount: parsed to `Decimal` — never `float`. Accepts `1234.56`, `1,234.56`, `1 234,56`,
     `1.234,56`, `(12.00)`, `AED 12.50`, `$12.50`. If both `,` and `.` occur, the right-most is the
     decimal separator; a lone `,` followed by 1–2 digits is a decimal comma, otherwise a thousands
     separator. `12.5` equals `12.50`;
   - currency: trimmed, upper-cased;
   - date: parsed best-effort (ISO, `dd.mm.yyyy`, `dd/mm/yyyy`, timestamps); never fatal.
2. **`INVALID_ROW`** — empty reference or unparseable amount. Reported, never silently dropped.
3. **`DUPLICATE_ORDER`** — orders sharing a reference: the first row (file order) is canonical,
   every other row is a duplicate.
4. **`DUPLICATE_PAYMENT`** — payments sharing a reference: the earliest by `payment_date` (then
   file order) is canonical, the rest are duplicates. Only the canonical payment is compared with
   the order, so a double capture never masquerades as an amount mismatch.
5. **Exact join** canonical order ↔ canonical payment on reference:
   - currencies differ → **`CURRENCY_MISMATCH`** (amounts are not compared across currencies);
   - `|payment − order| > tolerance` (default `0`) → **`AMOUNT_MISMATCH`**, with the signed
     difference;
   - otherwise → **`MATCHED`**.
6. **`MISSING_PAYMENT`** — canonical order with no payment reference;
   **`ORPHAN_PAYMENT`** — canonical payment with no order reference.
7. **Optional fallback** (off by default) — remaining `MISSING`/`ORPHAN` rows with identical
   amount and currency and dates within *N* days are paired one-to-one in file order and reported
   as **`FALLBACK_MATCHED`** ("review manually"). It never changes the outcome of rules 1–6.

Every input row appears exactly once in the report (tests enforce this).

## Input handling

- Two CSV files, up to 50 MB each.
- Encoding: BOM → `utf-8-sig`; strict UTF-8; `chardet` guess if confident; `cp1252` fallback.
  UTF-16 exports are handled.
- Delimiter: `,` `;` `\t` `|` — chosen by column-count consistency over the first 50 lines, so
  `;`-separated files with decimal commas are not mis-detected.
- All cells are read as strings; leading zeros in references survive.
- Ragged rows and duplicate header names are rejected with a line number instead of being
  "repaired" the way pandas would.

## Export

- **CSV** (UTF-8 with BOM for Excel) — exceptions only or all rows.
- **XLSX** — `Summary` sheet (counts, options), `Exceptions`, `All rows`; amounts are written as
  exact numeric cells, category column colour-coded, header frozen, auto-filter on.

Columns: `category, reference, order_row, payment_row, order_amount, payment_amount, difference,
order_currency, payment_currency, order_date, payment_date, explanation`.

## Project layout

```
app/            FastAPI app, engine, loaders, exporters, Jinja2/HTMX templates, CLI
tests/          pytest suite (engine rules, normalisation, CSV edge cases, exports, HTTP e2e, dataset)
examples/       12-row hand-written pair covering every category
demo-data/      1000-order synthetic dataset with planted errors + expected.json + generator
Dockerfile, docker-compose.yml
.github/workflows/ci.yml   ruff → pytest (3.11/3.12) → dataset check → docker build + container smoke test
```

## Development

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest
python demo-data/generate.py      # regenerate dataset (deterministic, seed fixed)
```

`demo-data/*.csv` and `expected.json` are derived from `generate.py`. CI fails if the committed
copies drift from the generator, and `sync-demo-data.yml` re-commits them whenever the generator
changes. If your checkout lacks them, the test suite generates them on first run.

## Limitations (read before trusting the output)

- **One reference = one order = one payment.** Partial captures, split shipments, instalments and
  refunds are reported as `AMOUNT_MISMATCH` / `DUPLICATE_PAYMENT`; the tool does not net them.
  Filter refunds/voids out of the PSP export first, or treat those categories as a review queue.
- **Exact reference match only.** Prefixed or truncated references (`ORD-1001` vs `1001`) are not
  reconciled unless you pre-clean the file. The fallback matcher is a heuristic and is labelled as
  such.
- **No FX.** Currency mismatches are flagged, never converted.
- **Amount tolerance is absolute** and applies within one currency; there is no percentage mode.
- **Ambiguous number formats.** `1,234` is read as one thousand two hundred thirty-four. If your
  export uses a decimal comma with three decimals, normalise it first.
- **Dates are informational** except for duplicate ordering and the fallback matcher; timezone
  boundaries are ignored.
- **Sessions live in process memory** (1 h TTL, 200 sessions). Files are not persisted; a restart
  drops them. Do not run multiple replicas behind a load balancer without sticky sessions.
- **No authentication.** Deploy behind your own SSO/VPN; the tool handles financial data.
- Whole files are loaded into memory; tested comfortably at tens of thousands of rows, not millions.

## License

MIT — see [LICENSE](LICENSE).
