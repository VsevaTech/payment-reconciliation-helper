# Demo data

Synthetic but realistic dataset for a UAE e-commerce merchant (AED-dominant, some USD/SAR/EUR),
generated deterministically by `generate.py` (fixed seed, idempotent). Legacy encodings
(cp1252, UTF-16, BOM) are exercised in `tests/test_csv_loader.py`.

| File            | Rows | Format                                                        |
|-----------------|-----:|---------------------------------------------------------------|
| `orders.csv`    | 1008 | UTF-8, comma, internal columns `Order ID, Customer, Amount, Currency, Date, Channel` |
| `payments.csv`  | 1025 | UTF-8, **semicolon**, **CRLF**, shuffled, PSP columns `psp_transaction_id, merchant_reference, amount, currency, payment_date, status, transaction_type, card_brand, merchant_name`; refunds have negative amounts |
| `expected.json` |    — | exact summary (with and without the transaction type), per-currency financials, planted references, showcase amounts |
| `verify.py`     |    — | checks a CLI `--json-full` or API (`include_rows=true`) payload against `expected.json` |

1000 random orders plus 6 hand-made "showcase" orders (all 100.00 AED) and 2 duplicated order
rows. Expected result with `transaction_type` mapped:

```
MATCHED                     976
MATCHED_SPLIT                 5   4 random (one 3-way) + ORD-2024-01002: 60.00 + 40.00
PARTIALLY_REFUNDED            4   3 random + ORD-2024-01004: 100.00 captured, 30.00 refunded → net 70.00
REFUNDED                      3   2 random + ORD-2024-01005: 100.00 captured, 100.00 refunded
MISSING_PAYMENT               6
ORPHAN_PAYMENT                6   5 captures + 1 refund without an order
AMOUNT_MISMATCH               3   (+0.01 / −0.10 / +10.00)
PARTIAL_PAYMENT               3   2 random + ORD-2024-01003: 60.00 + 35.00 → difference −5.00
CURRENCY_MISMATCH             2
POSSIBLE_DUPLICATE_CAPTURE    3   2 random + ORD-2024-01006: 100.00 + 100.00
VOIDED                        1   order whose only transaction is a VOID
DUPLICATE_ORDER               2   (order row exported twice)
```

`ORD-2024-01001` is the plain 100.00 → 100.00 `MATCHED` case.

Without the transaction type (`summary_without_transaction_type`): refund groups turn into
`AMOUNT_MISMATCH` (negative amounts are not netted), the voided order looks `MATCHED` — this
demonstrates why the column should be mapped.

Expectations are computed inside `generate.py` from what it planted, independently of the
engine. Verify:

```
python -m app.cli demo-data/orders.csv demo-data/payments.csv --json-full > out.json
python demo-data/verify.py out.json
python -m app.cli demo-data/orders.csv demo-data/payments.csv --json-full --no-transaction-type > legacy.json
python demo-data/verify.py legacy.json --without-transaction-type
```

`tests/test_demo_dataset.py` and CI run the same checks (counts, per-currency money, planted
references, showcase amounts, every source row reported exactly once). Regenerate with
`python demo-data/generate.py`.
