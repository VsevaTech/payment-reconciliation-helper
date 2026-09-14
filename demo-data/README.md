# Demo data

Synthetic but realistic dataset for a UAE e-commerce merchant (AED-dominant, some USD/SAR/EUR),
generated deterministically by `generate.py` (fixed seed, idempotent). Legacy encodings
(cp1252, UTF-16, BOM) are exercised in `tests/test_csv_loader.py`.

| File            | Rows | Format                                                        |
|-----------------|-----:|---------------------------------------------------------------|
| `orders.csv`    | 1002 | UTF-8, comma, internal columns `Order ID, Customer, Amount, Currency, Date, Channel` |
| `payments.csv`  | 1001 | UTF-8, **semicolon**, **CRLF**, shuffled, PSP columns `psp_transaction_id, merchant_reference, amount, currency, payment_date, status, card_brand, merchant_name` |
| `expected.json` |    — | exact summary the engine must produce, plus the planted references |

Planted discrepancies (1000 unique orders):

```
MATCHED              989
MISSING_PAYMENT        6
ORPHAN_PAYMENT         5
AMOUNT_MISMATCH        3   (+0.01 / −0.10 / +10.00)
CURRENCY_MISMATCH      2
DUPLICATE_PAYMENT      2   (same reference captured twice)
DUPLICATE_ORDER        2   (order row exported twice)
```

Verify:

```
python -m app.cli demo-data/orders.csv demo-data/payments.csv --json
```

`tests/test_demo_dataset.py` asserts the counts and that every planted reference lands in
its category. Regenerate with `python demo-data/generate.py`.
