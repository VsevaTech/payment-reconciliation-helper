# Examples

Small hand-written pair (17 order rows) for a quick look at the main categories. Orders are
comma-separated UTF-8; payments are semicolon-separated (delimiter is auto-detected) and carry a
`transaction_type` column (`CAPTURE` / `REFUND` / `CHARGEBACK`), which the CLI picks up automatically.

```
python -m app.cli examples/orders_small.csv examples/payments_small.csv
```

Expected result:

| Category                   | Count | Rows                                                             |
|----------------------------|------:|------------------------------------------------------------------|
| MATCHED                    |     6 | ORD-1001, 1003, 1006, 1008 (`12.5` = `12.50`), 1010, 1011        |
| MATCHED_SPLIT              |     1 | ORD-1012 — 100.00 paid as 60.00 + 40.00 (payment rows 12, 13)    |
| PARTIALLY_REFUNDED         |     1 | ORD-1014 — captured 100.00, refunded 30.00, net 70.00            |
| REFUNDED                   |     1 | ORD-1015 — captured 100.00, refunded 100.00, net 0.00            |
| MISSING_PAYMENT            |     2 | ORD-1004, ORD-1009                                               |
| ORPHAN_PAYMENT             |     1 | TXN-10 (ORD-9999)                                                |
| AMOUNT_MISMATCH            |     1 | ORD-1002 (99.99 vs 99.90)                                        |
| PARTIAL_PAYMENT            |     1 | ORD-1013 — 60.00 + 35.00 for 100.00, difference −5.00            |
| CURRENCY_MISMATCH          |     1 | ORD-1005 (SAR vs AED)                                            |
| POSSIBLE_DUPLICATE_CAPTURE |     1 | ORD-1007 — TXN-5 and TXN-6 both capture 2199.00                  |
| CHARGED_BACK               |     1 | ORD-1016 — captured 100.00, charged back 100.00, net 0.00        |
| DUPLICATE_ORDER            |     1 | second ORD-1006 row in orders                                    |

With `--no-transaction-type` every payment row is treated as a capture: ORD-1014 becomes
`AMOUNT_MISMATCH` (a negative amount is never netted silently), so does the ORD-1016 chargeback,
and ORD-1015 looks like a duplicate capture — which is why the column should be mapped when an
export mixes types.

For a realistic volume (1000+ rows, planted cases, semicolon/CRLF PSP export, shuffled rows)
see [`demo-data/`](../demo-data/).
