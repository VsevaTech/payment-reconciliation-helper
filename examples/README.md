# Examples

Small hand-written pair for a quick look at every category. Orders are comma-separated
UTF-8; payments are semicolon-separated (delimiter is auto-detected).

```
python -m app.cli examples/orders_small.csv examples/payments_small.csv
```

Expected result:

| Category            | Count | Rows                                                   |
|---------------------|------:|--------------------------------------------------------|
| MATCHED             |     7 | ORD-1001, 1003, 1006, 1007, 1008 (`12.5` = `12.50`), 1010, 1011 |
| MISSING_PAYMENT     |     2 | ORD-1004, ORD-1009                                     |
| ORPHAN_PAYMENT      |     1 | TXN-10 (ORD-9999)                                      |
| AMOUNT_MISMATCH     |     1 | ORD-1002 (99.99 vs 99.90)                              |
| CURRENCY_MISMATCH   |     1 | ORD-1005 (SAR vs AED)                                  |
| DUPLICATE_PAYMENT   |     1 | TXN-6 (second capture of ORD-1007)                     |
| DUPLICATE_ORDER     |     1 | second ORD-1006 row in orders                          |

For a realistic volume (1000+ rows, planted errors, cp1252 payments export) see
[`demo-data/`](../demo-data/).
