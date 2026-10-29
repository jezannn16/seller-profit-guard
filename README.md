# Seller Profit Guard

CSV-first marketplace SKU economics. This MVP has no seller account connection and does not change prices, ads, stock or listings. All money is parsed and calculated with `Decimal`.

## Implemented

- CSV preview (and optional XLSX preview when `openpyxl` is installed) with header mapping by case-insensitive column names, row-level validation errors, repeated-row detection, and a 20 MiB file ceiling.
- Idempotent re-import of an identical file using SHA-256; date+SKU primary key prevents overwriting previous imports. Import batches record accepted/rejected counts.
- Daily SKU analytics: gross sales, returns, net sales, marketplace fees, logistics, ad spend, COGS and contribution margin. Margin stays unknown when any required cost/return input is missing. If ad-attributed sales are supplied, dashboard also calculates ROAS and ACoS.
- Inventory cover estimate from latest stock / average daily sales; alert dry-run, cooldown log and explicit Telegram delivery with a delivery log.
- CSV export and a synthetic fixture with hand-checkable expected values.

## CSV format

Required columns: `date` (ISO `YYYY-MM-DD`), `sku`, `units`, `gross_sales`. Optional columns: `returns`, `fees`, `logistics`, `ad_spend`, `ad_sales`, `cogs`, `stock`, `avg_daily_units`. Empty returns or cost values are marked unknown; contribution margin is reported only when returns and all cost columns are supplied. ROAS and ACoS remain unknown without ad-attributed sales. One row per day and SKU.

## Quick start

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install pytest ruff
# Optional XLSX reader:
python -m pip install openpyxl
python seller_profit_guard.py preview fixtures/daily_sku.csv
python seller_profit_guard.py --db seller.sqlite3 import fixtures/daily_sku.csv
python seller_profit_guard.py --db seller.sqlite3 dashboard
python seller_profit_guard.py --db seller.sqlite3 alerts --min-margin 1000 --min-stock-days 7 --min-roas 3 --max-acos 0.35
python seller_profit_guard.py --db seller.sqlite3 export export.csv
pytest -q
```

Telegram alerts are sent only when the user explicitly runs `alerts --send-telegram`. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from a private shell or secret manager. The sender has a 10-second timeout and never retries non-idempotent sendMessage calls; ambiguous failures are logged as `unknown` and cooldown-suppressed to reduce duplicate delivery. `--commit-log` only records local preview entries and sends nothing.

Example using the placeholders in `.env.example` after replacing them locally:

```sh
set -a; . .env; set +a
python seller_profit_guard.py --db seller.sqlite3 alerts --min-margin 1000 --min-stock-days 7 --send-telegram
```

## Formulas and assumptions

- `net sales = gross sales − returns`.
- `contribution margin = net sales − fees − logistics − ad spend − COGS` only when returns and each cost field are present for every included row.
- `ROAS = ad-attributed sales / ad spend`; `ACoS = ad spend / ad-attributed sales`. General gross sales are never substituted for ad-attributed sales.
- Revenue is not profit. Taxes, storage, payment fees, discounts and other costs must be included in fees/logistics or added before interpreting margin.
- Inventory cover is a rough point-in-time estimate `latest stock / latest average daily units`; it does not model inbound shipments or demand variability.

## Limitations / planned

CSV is built in; XLSX support is optional through `openpyxl`. Separate returns/ad reports, UI dashboard and marketplace API sync are not implemented. Marketplace credentials were not available, so neither WB nor Ozon was selected. Alert mode is dry-run by default; explicit Telegram delivery requires credentials. Imports are append-only; overlapping date+SKU rows are rejected and do not overwrite history. CSV data is stored locally without encryption.

See [demo steps](docs/demo.md) for a synthetic end-to-end walkthrough. Official references for a future adapter: [Wildberries API token lifecycle](https://seller.wildberries.ru/instructions/ru/tj/material/how-to-create-update-or-delete-a-wb-api-token?recommended=true) and [Ozon Seller API documentation](https://docs.ozon.ru/global/api/). Neither adapter was selected because seller access credentials were unavailable.
