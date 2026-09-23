# Payment Reconciliation Helper

[![CI](https://github.com/VsevaTech/payment-reconciliation-helper/actions/workflows/ci.yml/badge.svg)](https://github.com/VsevaTech/payment-reconciliation-helper/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![license](https://img.shields.io/badge/license-MIT-green)

Upload an internal **orders** export and a PSP / acquirer **payments** export, map a handful of
columns, and get a deterministic list of discrepancies — missing payments, orphan captures, amount
and currency mismatches, split and partial payments, refunds, possible duplicate captures — plus a
per-currency financial summary and an XLSX/CSV report you can hand to finance.

```
orders.csv + payments.csv → map columns → reconcile → financial summary + exceptions → export
```

Demo dataset result (`demo-data/`, transaction type mapped):

```
MATCHED                     976      MISSING_PAYMENT             6
MATCHED_SPLIT                 5      ORPHAN_PAYMENT              6
PARTIALLY_REFUNDED            4      AMOUNT_MISMATCH             3
REFUNDED                      3      PARTIAL_PAYMENT             3
                                     CURRENCY_MISMATCH           2
                                     POSSIBLE_DUPLICATE_CAPTURE  3
                                     VOIDED                      1
                                     DUPLICATE_ORDER             2
```

### In one example

```
Order ORD-1 100.00 AED
  payments: capture 60.00 (row 3) + capture 40.00 (row 7)
  → MATCHED_SPLIT   captured 100.00 · difference 0.00 · payment_rows 3;7

Order ORD-2 100.00 AED
  payments: capture 100.00 (row 4, type CAPTURE) + refund 30.00 (row 9, type REFUND)
  → PARTIALLY_REFUNDED   captured 100.00 · refunded 30.00 · net 70.00 · payment_rows 4;9
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
| Amount mismatch | PSP fee deducted before export, rounding in FX conversion, coupon applied post-checkout |
| Split / partial payment | Split tender (two cards), instalments, partial capture of a multi-shipment order |
| Refund | Customer return, goodwill credit, cancelled line item after capture |
| Currency mismatch | Dynamic Currency Conversion at the terminal, multi-currency pricing bug |
| Possible duplicate capture | Double-click on "Pay", retry storm on a timeout, manual capture after auto-capture |
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
   `Currency → currency`, `Date → payment_date` and, for payments, `transaction_type`. Confirm.
4. **Run reconciliation** → the financial summary shows orders total, captured, refunded, net
   captured, voided and unreconciled amount **per currency**; the filter chips (Matched, Split,
   Partial, Refunded, Missing, Amount mismatch, Duplicates, Orphans, …) and the currency chips
   narrow the table. Exact expectations: [`demo-data/expected.json`](demo-data/expected.json).
5. **Export XLSX** → `reconciliation.xlsx` with `Summary` (counts + financial summary),
   `Exceptions` and `All rows` sheets. **Financial summary CSV** exports the per-currency table.

The same flow without a browser:

```bash
python -m app.cli demo-data/orders.csv demo-data/payments.csv --xlsx reconciliation.xlsx
python -m app.cli demo-data/orders.csv demo-data/payments.csv --json-full > out.json
python demo-data/verify.py out.json          # asserts counts, money, planted refs, row coverage
```

or over HTTP:

```bash
curl -s http://localhost:8000/api/reconcile \
  -F orders=@demo-data/orders.csv -F payments=@demo-data/payments.csv \
  -F orders_reference="Order ID" -F orders_amount=Amount -F orders_currency=Currency -F orders_date=Date \
  -F payments_reference=merchant_reference -F payments_amount=amount \
  -F payments_currency=currency -F payments_date=payment_date \
  -F payments_transaction_type=transaction_type -F include_rows=false | jq '.summary, .financials'
```

## Matching rules

All rules are deterministic, run in this order, and each result row carries a one-line
`explanation` plus the 1-based source row numbers (`order_row`, and `payment_rows` listing
**every** contributing payment row) so it can be checked against the original files.

1. **Normalisation**
   - reference: trimmed; case-sensitive unless *case-insensitive* is ticked;
   - amount: parsed to `Decimal` — never `float`; at most 15 integer digits and 4 decimals
     (larger values are `INVALID_ROW`, which keeps every sum exact). Accepts `1234.56`,
     `1,234.56`, `1 234,56`, `1.234,56`, `(12.00)`, `AED 12.50`, `$12.50`. If both `,` and `.` occur, the right-most is the
     decimal separator; a lone `,` followed by 1–2 digits is a decimal comma, otherwise a thousands
     separator. `12.5` equals `12.50`;
   - currency: trimmed, upper-cased;
   - date: parsed best-effort (ISO, `dd.mm.yyyy`, `dd/mm/yyyy`, timestamps); never fatal;
   - transaction type (payments, optional): see [Transaction-type mapping](#transaction-type-mapping).
2. **`INVALID_ROW`** — empty reference, unparseable amount, or (type mapped) an unknown/empty type
   or a negative amount on a `PAYMENT` row. Reported, never silently dropped.
3. **`DUPLICATE_ORDER`** — orders sharing a reference: the first row (file order) is canonical,
   every other row is a duplicate.
4. **Payment groups** — all valid payment rows sharing a reference form one group, ordered by
   `payment_date`, then file order. The group is split into captures (`PAYMENT`), refunds
   (`REFUND`) and voids (`VOID`). Repeated references are **not** automatically duplicates.
5. **Canonical order ↔ payment group** on the exact reference, first matching rule wins:

   | Situation | Category |
   |---|---|
   | any payment currency ≠ order currency | **`CURRENCY_MISMATCH`** — amounts never summed or compared across currencies |
   | no captures, only voids | **`VOIDED`** |
   | refunds > captures (incl. refund with no capture) | **`REFUND_EXCEEDS_CAPTURE`** |
   | 1 capture, capture = order (± tolerance) | **`MATCHED`** |
   | 1 capture, otherwise | **`AMOUNT_MISMATCH`** (signed difference) |
   | ≥2 captures, sum equals order | **`MATCHED_SPLIT`** |
   | ≥2 captures, sum below order | **`PARTIAL_PAYMENT`** (negative difference) |
   | ≥2 captures, sum above order, surplus explained by a repeated capture | **`POSSIBLE_DUPLICATE_CAPTURE`** |
   | ≥2 captures, sum above order, not explained | **`AMOUNT_MISMATCH`** |

   Then, if the captures reconciled (`MATCHED` / `MATCHED_SPLIT`) and something was refunded:
   refunded = captured → **`REFUNDED`**, otherwise **`PARTIALLY_REFUNDED`**. A refund never hides a
   capture problem: a partial payment that was also refunded stays `PARTIAL_PAYMENT`, with the
   refund in the columns and the explanation.
6. **`MISSING_PAYMENT`** — canonical order with no payment reference;
   **`ORPHAN_PAYMENT`** — payment group with no order reference (one row per reference, listing all
   of its source rows).
7. **Optional fallback** (off by default) — remaining `MISSING` orders and single-capture `ORPHAN`
   groups with identical amount and currency and dates within *N* days are paired one-to-one in
   file order and reported as **`FALLBACK_MATCHED`** ("review manually"). It never changes the
   outcome of rules 1–6.

Every input row appears in exactly one result row (tests and `demo-data/verify.py` enforce this).
`difference` is always *captured − ordered*; refunds are reported separately, never netted into it.

### Split and partial payments

Several captures with the same reference are summed (Decimal) and compared with the order once:
`60.00 + 40.00` for a `100.00` order is `MATCHED_SPLIT`; `60.00 + 35.00` is `PARTIAL_PAYMENT` with
difference `-5.00`. The amount tolerance applies to the sum. A **single** capture that differs from
the order is still `AMOUNT_MISMATCH`, exactly as before — one short payment cannot be told apart
from a wrong amount.

### Duplicate captures

When captures exceed the order, the engine looks for a repeated capture:

1. if one capture alone equals the order, the earliest such capture is the original and every
   other capture is flagged (`100 + 100` for `100`, or `100 + 999` for `100`);
2. otherwise captures that repeat an earlier capture's exact amount are flagged, but only if
   dropping them makes the rest equal the order (`60 + 40 + 40` for `100`).

That is **`POSSIBLE_DUPLICATE_CAPTURE`**; `duplicate_payment_rows` names the suspect rows (highlighted
in the UI). An unexplained surplus (`60 + 50` for `100`) is an `AMOUNT_MISMATCH`. "Possible" is
deliberate: a second capture can be legitimate (e.g. a re-shipped item) — confirm against the PSP.

### Refunds and voids

With a mapped transaction-type column:

- `REFUND` rows reduce **net**, not captured: `captured_amount`, `refunded_amount`,
  `net_amount = captured − refunded` are separate columns. The sign of a refund amount in the export
  does not matter (`-30.00` and `30.00` are both a refund of 30.00).
- `VOID` rows are transactions cancelled before settlement: counted in `voided_amount`, excluded
  from captured and net. An order with only voids is `VOIDED`; a void next to a capture is
  informational (`100 CAPTURE + 100 VOID` for `100` is `MATCHED`).

Example: capture `100.00` + refund `30.00` → `PARTIALLY_REFUNDED`, captured 100.00, refunded
30.00, net 70.00. Capture `100.00` + refund `100.00` → `REFUNDED`, net 0.00.

### Transaction-type mapping

Optional, payments only (`payments_transaction_type` in the API, `--payments-transaction-type` in
the CLI, a select on the mapping page). The header is guessed from names like `transaction_type`,
`txn_type`, `operation_type`, `type` — never from `status`, because PSP statuses mix outcomes
(`FAILED`, `PENDING`) with types. A guessed column is only pre-selected (UI) or used (CLI) when all
of its values are recognised types; otherwise the legacy mode applies and the CLI prints a note.
An explicitly mapped column is always used. Values are matched case-insensitively, ignoring
spaces and `-`:

| Canonical | Accepted values |
|---|---|
| `PAYMENT` | `PAYMENT`, `CAPTURE`, `CAPTURED`, `SALE`, `PURCHASE`, `CHARGE`, `DEBIT` |
| `REFUND` | `REFUND`, `REFUNDED`, `PARTIAL_REFUND`, `CREDIT`, `RETURN` |
| `VOID` | `VOID`, `VOIDED`, `CANCEL`, `CANCELLED`, `CANCELED`, `REVERSAL`, `REVERSED`, `AUTH_REVERSAL` |

Anything else (`AUTH`, `CHARGEBACK`, `FAILED`, empty) makes that row `INVALID_ROW` instead of being
guessed.

**Not mapped → previous behaviour:** every payment row is a capture with its signed amount. Split,
partial and duplicate-capture detection still work; refunds and voids are *not* identified, the
refunded/voided columns stay empty and the UI says so. A group that contains a negative amount is
reported as `AMOUNT_MISMATCH` with a hint to map the column, never silently netted. On the demo
dataset the difference is visible: see `summary_without_transaction_type` in `expected.json`
(refund groups become `AMOUNT_MISMATCH`, the voided order looks `MATCHED`).

### Financial summary

The results page, the API (`financials`), the CLI and the XLSX `Summary` sheet show one row per
currency — **different currencies are never added together**:

| Column | Meaning |
|---|---|
| Orders total | sum of unique (canonical) valid orders in that currency; duplicate order rows excluded |
| Captured | sum of `PAYMENT` rows (all rows when no type is mapped), orphans included |
| Refunded | sum of `REFUND` rows (absolute) |
| Net captured | captured − refunded |
| Voided | sum of `VOID` rows (absolute), informational |
| Unreconciled | money in exception rows that does not tie out, see below |

Unreconciled, per result row, in the currency of the money concerned:
`MISSING_PAYMENT` / `VOIDED` → order amount; `AMOUNT_MISMATCH`, `PARTIAL_PAYMENT`,
`POSSIBLE_DUPLICATE_CAPTURE` → `|captured − order|`; `REFUND_EXCEEDS_CAPTURE` →
`|captured − order| + (refunded − captured)`; `ORPHAN_PAYMENT` → gross captured + refunded
(never netted); `CURRENCY_MISMATCH` → the order amount in the order currency **and** the payments in each foreign
currency. `MATCHED`, `MATCHED_SPLIT`, `PARTIALLY_REFUNDED`, `REFUNDED` and `FALLBACK_MATCHED`
contribute 0; `DUPLICATE_ORDER` and `INVALID_ROW` are counted, not valued. Without a mapped
transaction type, un-netted negative amounts in an exception group are added as well. Amounts
are absolute, so unreconciled is never negative.

### Result filters

Chips on the results page (and `?filter=` on `/s/{id}/result`): `matched`, `split`, `partial`,
`refunded` (partially + fully), `missing`, `amount_mismatch`, `currency_mismatch`, `duplicates`
(possible duplicate captures + duplicate orders), `orphans`, `refund_issues`, `voided`, `fallback`,
`invalid`, `all`. `?currency=AED` narrows any view; `?category=` still selects a single category.
The default view is all exceptions; `MATCHED`, `MATCHED_SPLIT`, `PARTIALLY_REFUNDED` and `REFUNDED`
are reconciled and are not exceptions.

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
- **Financial summary CSV** — the per-currency table.
- **XLSX** — `Summary` sheet (counts, options, per-currency financial summary), `Exceptions`,
  `All rows`; amounts are written as exact numeric cells, category column colour-coded, header
  frozen, auto-filter on.

Columns: `category, reference, order_row, payment_row, payment_rows, order_amount, payment_amount,
captured_amount, refunded_amount, voided_amount, net_amount, difference, order_currency,
payment_currency, transaction_types, duplicate_payment_rows, order_date, payment_date, explanation`.

`payment_row` is the first contributing capture (kept for compatibility); `payment_rows` lists every
contributing row, `;`-separated. `payment_amount` is the sum of captures — the single payment
amount when there is one, as before.

## Upgrading from 0.1

- `DUPLICATE_PAYMENT` is gone. A repeated reference is now evaluated as a group:
  `MATCHED_SPLIT`, `PARTIAL_PAYMENT`, `POSSIBLE_DUPLICATE_CAPTURE` or `AMOUNT_MISMATCH`, one result
  row per order (previously one row per extra payment).
- New categories: `MATCHED_SPLIT`, `PARTIALLY_REFUNDED`, `REFUNDED`, `PARTIAL_PAYMENT`,
  `POSSIBLE_DUPLICATE_CAPTURE`, `REFUND_EXCEEDS_CAPTURE`, `VOIDED`.
- `CURRENCY_MISMATCH` rows no longer carry a cross-currency `difference`.
- Amounts with more than 15 integer digits or more than 4 decimals are now `INVALID_ROW`.
- New export columns (see above); existing columns keep their names and meaning.
- API: new optional form field `payments_transaction_type`; responses add `financials` and
  `transaction_type_mapped`. CLI: `--payments-transaction-type`, `--no-transaction-type`,
  `--json-full`; `--json` still prints the category summary only.

## Project layout

```
app/            FastAPI app, engine, loaders, exporters, Jinja2/HTMX templates, CLI
tests/          pytest suite (engine rules, split/refund rules, normalisation, CSV edge cases,
                exports, HTTP e2e, dataset)
examples/       small hand-written pair (16 order rows) covering the main categories
demo-data/      1006-order synthetic dataset with planted cases + expected.json + generator + verify.py
Dockerfile, docker-compose.yml
.github/workflows/ci.yml   ruff → pytest (3.11/3.12) → dataset checks (typed + legacy) →
                           docker build + container API smoke test with demo-data/verify.py
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

- **Grouping is by reference only.** Payments are tied to an order solely through the exact
  reference. There is no link between a refund/void and the specific capture it reverses (no
  `psp_transaction_id` / `original_transaction_id` matching), so a refund is attributed to the
  order, not to a capture.
- **Split vs. partial vs. mismatch is decided on sums.** One short payment is `AMOUNT_MISMATCH`, not
  `PARTIAL_PAYMENT`; several captures that overshoot without a repeated amount are
  `AMOUNT_MISMATCH`. Duplicate detection is a heuristic ("possible") based on equal amounts; it
  does not look at card, time of day or PSP ids.
- **Transaction types are a closed list.** Chargebacks, authorisations, fees, payouts and
  adjustments are not modelled; with the column mapped they become `INVALID_ROW`. Filter them out
  or relabel them first. A void is assumed to be a separate cancelled transaction; if your PSP
  exports a void *in addition to* the original capture row, that capture still counts.
- **No transaction type → no refunds.** Without the column, refunds are read as (negative or
  positive) captures and flagged, not netted. Map the column whenever the export mixes types.
- **Refunds are compared exactly** with captures (no tolerance); the amount tolerance is absolute,
  within one currency, applied to capture sums; there is no percentage mode.
- **Exact reference match only.** Prefixed or truncated references (`ORD-1001` vs `1001`) are not
  reconciled unless you pre-clean the file. The fallback matcher is a heuristic, pairs only single
  captures, and is labelled as such.
- **No FX.** Currency mismatches are flagged, never converted; totals are never mixed across
  currencies. Mixed-currency payment groups are reported without sums.
- **Ambiguous number formats.** `1,234` is read as one thousand two hundred thirty-four. If your
  export uses a decimal comma with three decimals, normalise it first.
- **Dates are informational** except for ordering within a group and the fallback matcher; timezone
  boundaries are ignored.
- **Sessions live in process memory** (1 h TTL, 200 sessions). Files are not persisted; a restart
  drops them. Do not run multiple replicas behind a load balancer without sticky sessions.
- **No authentication.** Deploy behind your own SSO/VPN; the tool handles financial data.
- Whole files are loaded into memory; tested comfortably at tens of thousands of rows, not millions.

## License

MIT — see [LICENSE](LICENSE).
