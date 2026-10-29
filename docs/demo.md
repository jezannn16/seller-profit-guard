# Demo: Seller Profit Guard

Run from this folder using the synthetic CSV fixture:

```sh
python seller_profit_guard.py preview fixtures/daily_sku.csv
python seller_profit_guard.py --db /tmp/seller-profit-demo.sqlite3 import fixtures/daily_sku.csv
python seller_profit_guard.py --db /tmp/seller-profit-demo.sqlite3 dashboard
python seller_profit_guard.py --db /tmp/seller-profit-demo.sqlite3 alerts --min-margin 6000 --min-stock-days 7 --min-roas 10
```

Expected behavior: preview accepts three rows; SKU-ALPHA shows 17,500 net sales and 5,000 contribution margin, ROAS from the supplied attributed sales, and a six-day inventory estimate. SKU-BETA margin and ROAS remain unknown because the fixture omits COGS and ad-attributed sales. Alerts print `DRY-RUN` messages and do not send anything.
