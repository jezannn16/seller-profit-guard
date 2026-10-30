import csv
import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

import seller_profit_guard as seller_module
from seller_profit_guard import (
    calculate,
    connect,
    evaluate_alerts,
    export_csv,
    fact_summary,
    import_csv,
    parse_column_mappings,
    parse_csv,
    parse_xlsx,
    send_telegram_alerts,
)


def test_known_margin_and_missing_costs(tmp_path):
    db = connect(tmp_path / "seller.db")
    import_csv(db, "fixtures/daily_sku.csv")
    rows = db.execute("select * from daily_sku where sku='SKU-ALPHA'").fetchall()
    m = calculate(rows)
    assert m.gross_sales == Decimal(18000)
    assert m.net_sales == Decimal(17500)
    assert m.return_rate == Decimal(500) / Decimal(18000)
    assert m.contribution_margin == Decimal(5000)
    assert m.roas == Decimal(14000) / Decimal(1200)
    assert m.acos == Decimal(1200) / Decimal(14000)
    beta = calculate(db.execute("select * from daily_sku where sku='SKU-BETA'").fetchall())
    assert beta.contribution_margin is None and not beta.margin_complete


def test_zero_sales_has_no_divide_by_zero_return_rate(tmp_path):
    db = connect(tmp_path / "seller.db")
    path = tmp_path / "zero.csv"
    path.write_text(
        "date,sku,units,gross_sales,returns,fees,logistics,ad_spend,cogs\n"
        "2026-10-28,SKU-ZERO,0,0,0,0,0,0,0\n",
        encoding="utf-8",
    )
    import_csv(db, path)
    metric = calculate(db.execute("select * from daily_sku where sku='SKU-ZERO'").fetchall())
    assert metric.net_sales == 0 and metric.return_rate is None


def test_export_csv_neutralizes_formula_like_sku(tmp_path):
    path = tmp_path / "formula.csv"
    path.write_text(
        "date,sku,units,gross_sales,returns,fees,logistics,ad_spend,cogs\n"
        "2026-10-28,=HYPERLINK(\"https://example.invalid\"),1,100,0,0,0,0,10\n",
        encoding="utf-8",
    )
    db = connect(tmp_path / "seller.db")
    import_csv(db, path)
    exported = tmp_path / "safe.csv"

    export_csv(db, exported)

    with exported.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    assert rows[0]["sku"].startswith("'=HYPERLINK")
    if os.name == "posix":
        assert stat.S_IMODE(exported.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode check")
def test_database_file_permissions_are_owner_only(tmp_path):
    path = tmp_path / "private-seller.sqlite3"
    db = connect(path)
    db.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_import_is_idempotent_duplicate_rows_rejected(tmp_path):
    db = connect(tmp_path / "seller.db")
    first = import_csv(db, "fixtures/daily_sku.csv")
    second = import_csv(db, "fixtures/daily_sku.csv")
    assert first["accepted"] == 3 and not first["duplicate_batch"]
    assert second["duplicate_batch"] and second["accepted"] == 0


def test_import_persists_only_observed_normalized_daily_facts(tmp_path):
    db = connect(tmp_path / "seller.db")
    import_csv(db, "fixtures/daily_sku.csv")
    assert db.execute("select count(*) from products").fetchone()[0] == 2
    assert db.execute("select count(*) from daily_returns").fetchone()[0] == 3
    assert db.execute("select count(*) from advertising_daily").fetchone()[0] == 3
    assert db.execute("select count(*) from marketplace_fees_daily").fetchone()[0] == 3
    assert db.execute("select count(*) from logistics_costs_daily").fetchone()[0] == 3
    assert db.execute("select count(*) from inventory_snapshots").fetchone()[0] == 3
    assert db.execute("select count(*) from orders").fetchone()[0] == 0
    assert db.execute("select count(*) from order_items").fetchone()[0] == 0
    assert sum((Decimal(row[0]) for row in db.execute("select amount from daily_returns")), Decimal(0)) == Decimal(500)


def test_legacy_database_migration_backfills_only_known_values(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""
      CREATE TABLE import_batches(id INTEGER PRIMARY KEY,source TEXT,content_hash TEXT,imported_at TEXT,accepted INTEGER,rejected INTEGER);
      CREATE TABLE daily_sku(date TEXT,sku TEXT,units TEXT,gross_sales TEXT,returns TEXT,fees TEXT,logistics TEXT,
        ad_spend TEXT,ad_sales TEXT,cogs TEXT,missing_fields TEXT,stock TEXT,avg_daily_units TEXT,batch_id INTEGER,
        PRIMARY KEY(date,sku));
      CREATE TABLE alert_log(id INTEGER PRIMARY KEY,alert_key TEXT,sent_at TEXT,mode TEXT);
      INSERT INTO import_batches VALUES(1,'legacy.csv','legacy-hash','2026-01-01T00:00:00+00:00',1,0);
      INSERT INTO daily_sku VALUES('2026-01-01','OLD-SKU','1','100','0','10','5','2',NULL,NULL,
        'returns,ad_sales,cogs','8','2',1);
    """)
    old.close()
    db = connect(path)
    assert db.execute("select sku,first_seen,last_seen from products").fetchone()[:] == (
        "OLD-SKU", "2026-01-01", "2026-01-01"
    )
    assert db.execute("select amount from marketplace_fees_daily").fetchone()[0] == "10"
    assert db.execute("select amount from logistics_costs_daily").fetchone()[0] == "5"
    ad = db.execute("select spend,attributed_sales from advertising_daily").fetchone()
    assert tuple(ad) == ("2", None)
    assert db.execute("select count(*) from daily_returns").fetchone()[0] == 0
    assert db.execute("select count(*) from inventory_snapshots").fetchone()[0] == 1
    assert db.execute("select count(*) from orders").fetchone()[0] == 0
    db.close()
    db = connect(path)
    assert db.execute("select count(*) from marketplace_fees_daily").fetchone()[0] == 1
    assert db.execute("select count(*) from products").fetchone()[0] == 1


def test_order_line_csv_creates_orders_only_from_explicit_ids_and_aggregates_with_decimal(tmp_path):
    day = date.today().isoformat()
    source = tmp_path / "orders.csv"
    source.write_text(
        "date,sku,units,gross_sales,returns,fees,logistics,ad_spend,ad_sales,cogs,stock,avg_daily_units,order_id,order_item_id\n"
        f"{day},SKU-X,1,10.1,0.1,0.1,0.1,0.1,1.1,3.3,20,2,ORDER-1,LINE-1\n"
        f"{day},SKU-X,2,20.2,0.2,0.2,0.2,0.2,2.2,6.6,20,2,ORDER-2,LINE-2\n",
        encoding="utf-8",
    )
    db = connect(tmp_path / "seller.db")
    result = import_csv(db, source)
    assert result["accepted"] == 2 and result["rejected"] == 0
    metric = calculate(db.execute("select * from daily_sku where sku='SKU-X'").fetchall())
    assert metric.gross_sales == Decimal("30.3")
    assert metric.returns == Decimal("0.3") and metric.net_sales == Decimal("30.0")
    assert metric.fees == Decimal("0.3") and metric.logistics == Decimal("0.3")
    assert metric.ad_spend == Decimal("0.3") and metric.ad_sales == Decimal("3.3")
    assert metric.cogs == Decimal("9.9") and metric.contribution_margin == Decimal("19.2")
    assert db.execute("select count(*) from orders").fetchone()[0] == 2
    assert db.execute("select count(*) from order_items").fetchone()[0] == 2
    assert [tuple(row) for row in db.execute("select units,gross_sales from order_items order by order_id")] == [
        ("1", "10.1"), ("2", "20.2")
    ]


def test_order_item_ids_require_explicit_orders_and_repeated_order_rows_are_detected(tmp_path):
    source = tmp_path / "orders.csv"
    source.write_text(
        "date,sku,units,gross_sales,order_item_id\n2026-10-28,SKU-X,1,10,LINE-1\n", encoding="utf-8"
    )
    rows, errors = parse_csv(source)
    assert rows == [] and "requires order_id" in errors[0]["error"]


def test_conflicting_order_identity_rejects_all_its_rows_without_partial_facts(tmp_path):
    source = tmp_path / "conflict.csv"
    source.write_text(
        "date,sku,units,gross_sales,order_id\n"
        "2026-10-28,SKU-X,1,10,ORDER-1\n"
        "2026-10-29,SKU-Y,1,20,ORDER-1\n",
        encoding="utf-8",
    )
    db = connect(tmp_path / "seller.db")
    imported = import_csv(db, source)
    assert imported["accepted"] == 0 and imported["rejected"] == 2
    assert db.execute("select count(*) from daily_sku").fetchone()[0] == 0
    assert db.execute("select count(*) from orders").fetchone()[0] == 0
    assert db.execute("select count(*) from products").fetchone()[0] == 0


def test_unexpected_failure_rolls_back_entire_batch_without_backfill(tmp_path, monkeypatch):
    source = tmp_path / "batch.csv"
    source.write_text(
        "date,sku,units,gross_sales,returns,fees,logistics,ad_spend,cogs\n"
        "2026-10-28,SKU-A,1,10,1,1,1,1,1\n"
        "2026-10-29,SKU-B,1,20,2,2,2,2,2\n",
        encoding="utf-8",
    )
    db = connect(tmp_path / "seller.db")
    original = seller_module._record_observed_facts
    calls = 0

    def fail_on_second_group(connection, batch, daily, rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected write failure")
        original(connection, batch, daily, rows)

    monkeypatch.setattr(seller_module, "_record_observed_facts", fail_on_second_group)
    with pytest.raises(sqlite3.OperationalError, match="injected write failure"):
        import_csv(db, source)

    for table in ("import_batches", "daily_sku", "products", "daily_returns", "marketplace_fees_daily"):
        assert db.execute(f"select count(*) from {table}").fetchone()[0] == 0


def test_row_level_errors_cover_bad_date_negative_stock_and_duplicate(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "date,sku,units,gross_sales,stock\n"
        "not-a-date,SKU-X,1,100,2\n"
        "2026-10-28,SKU-X,1,100,-1\n"
        "2026-10-28,SKU-Y,1,100,2\n"
        "2026-10-28,SKU-Y,1,100,3\n",
        encoding="utf-8",
    )
    rows, errors = parse_csv(path)
    assert [row["sku"] for row in rows] == ["SKU-Y"]
    assert len(errors) == 3
    assert "date" in errors[0]["error"]
    assert "negative" in errors[1]["error"]
    assert "duplicate" in errors[2]["error"]


def test_csv_extra_fields_are_reported_per_row_and_valid_rows_still_imported(tmp_path):
    path = tmp_path / "ragged.csv"
    path.write_text(
        "date,sku,units,gross_sales\n"
        "2026-10-28,SKU-GOOD,2,1200\n"
        "2026-10-28,SKU-BAD,1,500,unexpected\n"
        "2026-10-28,SKU-ALSO-GOOD,1,300\n",
        encoding="utf-8",
    )

    preview_result = seller_module.preview(path)

    assert preview_result["valid_rows"] == 2
    assert preview_result["rejected_rows"] == 1
    assert preview_result["errors"] == [{
        "line": 3,
        "error": "row has 1 extra field(s) beyond the 4 headers",
    }]

    db = connect(tmp_path / "ragged.db")
    result = import_csv(db, path)
    assert result["accepted"] == 2
    assert result["rejected"] == 1
    assert result["errors"] == [{
        "line": 3,
        "error": "row has 1 extra field(s) beyond the 4 headers",
    }]
    assert [row[0] for row in db.execute("SELECT sku FROM daily_sku ORDER BY sku")] == [
        "SKU-ALSO-GOOD", "SKU-GOOD",
    ]
    assert db.execute("SELECT accepted,rejected FROM import_batches").fetchone()[:] == (2, 1)


def test_ambiguous_duplicate_headers_fail_before_mapping(tmp_path):
    path = tmp_path / "ambiguous.csv"
    path.write_text(
        "date,sku,units,gross_sales,Gross_Sales\n2026-10-28,SKU-X,1,100,200\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate column headers"):
        parse_csv(path)


@pytest.mark.parametrize("column,value", [("gross_sales", "-100"), ("fees", "-1"), ("ad_spend", "-2"), ("cogs", "-3")])
def test_negative_revenue_and_cost_inputs_are_rejected(tmp_path, column, value):
    headers = ["date", "sku", "units", "gross_sales", "fees", "ad_spend", "cogs"]
    row = ["2026-10-28", "SKU-X", "1", "100", "0", "0", "0"]
    row[headers.index(column)] = value
    path = tmp_path / "negative.csv"
    path.write_text(",".join(headers) + "\n" + ",".join(row) + "\n", encoding="utf-8")

    rows, errors = parse_csv(path)

    assert not rows
    assert len(errors) == 1
    assert f"{column} cannot be negative" in errors[0]["error"]


def test_xlsx_import_when_optional_dependency_is_available(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from datetime import date

    path = tmp_path / "report.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append([
        "date", "sku", "units", "gross_sales", "returns", "fees", "logistics", "ad_spend", "ad_sales",
        "cogs", "stock", "avg_daily_units", "order_id",
    ])
    sheet.append([date(2026, 10, 28), "SKU-X", 2, 1200, 0, 10, 20, 30, 900, 400, 18, 2, "XLSX-ORDER"])
    workbook.save(path)
    workbook.close()
    rows, errors = parse_xlsx(path)
    assert not errors and rows[0]["date"] == "2026-10-28" and rows[0]["sku"] == "SKU-X"
    db = connect(tmp_path / "xlsx.db")
    assert import_csv(db, path)["accepted"] == 1
    facts = fact_summary(db)
    assert facts["products"] == facts["orders"] == facts["order_items"] == 1
    assert facts["daily_returns"] == facts["advertising_daily"] == facts["marketplace_fees_daily"] == 1
    assert facts["logistics_costs_daily"] == facts["inventory_snapshots"] == 1


def test_csv_custom_header_mapping_and_mapping_aware_idempotency(tmp_path):
    path = tmp_path / "localized.csv"
    path.write_text(
        "День,Артикул,Продажи,Шт\n2026-10-28,SKU-X,1200,2\n", encoding="utf-8"
    )
    mapping = parse_column_mappings(
        ["date=День", "sku=Артикул", "gross_sales=Продажи", "units=Шт"]
    )
    rows, errors = parse_csv(path, mapping)
    assert errors == [] and rows[0]["gross_sales"] == "1200" and rows[0]["sku"] == "SKU-X"
    db = connect(tmp_path / "seller.db")
    assert import_csv(db, path, mapping)["accepted"] == 1
    assert import_csv(db, path, mapping)["duplicate_batch"]
    case_variant = {key: value.swapcase() for key, value in mapping.items()}
    assert import_csv(db, path, case_variant)["duplicate_batch"]  # Header matching and batch identity are case-insensitive.
    with pytest.raises(ValueError, match="mapped more than once"):
        parse_column_mappings(["sku=Артикул", "sku=Код"])


def test_invalid_row_and_inventory_alert_dry_run(tmp_path):
    rows, errors = parse_csv("fixtures/daily_sku.csv")
    assert rows and not errors
    db = connect(tmp_path / "seller.db")
    import_csv(db, "fixtures/daily_sku.csv")
    alerts = evaluate_alerts(
        db, Decimal(100000), Decimal(7), dry_run=True, minimum_roas=Decimal(15), maximum_acos=Decimal("0.05")
    )
    assert any("SKU-ALPHA" in a for a in alerts)
    assert any("ROAS" in a for a in alerts)
    assert any("ACoS" in a for a in alerts)
    assert db.execute("select count(*) from alert_log").fetchone()[0] == 0


def test_roas_decline_and_acos_growth_compare_consecutive_periods(tmp_path):
    path = tmp_path / "trend.csv"
    start = date.today() - timedelta(days=13)
    lines = ["date,sku,units,gross_sales,returns,fees,logistics,ad_spend,ad_sales,cogs"]
    for day in range(14):
        ad_sales = 100 if day < 7 else 50
        lines.append(f"{start + timedelta(days=day)},SKU-TREND,1,100,0,0,0,10,{ad_sales},1")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    db = connect(tmp_path / "trend.db")
    import_csv(db, path)

    alerts = evaluate_alerts(db, Decimal(-999999), Decimal(0), trend_days=7)
    assert alerts == [
        "SKU-TREND: ROAS declined from 10.00 to 5.00 across consecutive 7-day periods",
        "SKU-TREND: ACoS increased from 10.00% to 20.00% across consecutive 7-day periods",
    ]
    combined = evaluate_alerts(db, Decimal(-999999), Decimal(0), minimum_roas=Decimal(8),
        maximum_acos=Decimal("0.12"), trend_days=7)
    assert sum("ROAS" in alert for alert in combined) == 1
    assert sum("ACoS" in alert for alert in combined) == 1
    logged = evaluate_alerts(db, Decimal(-999999), Decimal(0), dry_run=False, cooldown_hours=48, trend_days=7)
    assert logged == alerts
    assert evaluate_alerts(db, Decimal(-999999), Decimal(0), cooldown_hours=48, trend_days=7) == []
    with pytest.raises(ValueError, match="Cooldown"):
        evaluate_alerts(db, Decimal(0), Decimal(0), cooldown_hours=-1)


def test_rejected_import_rows_raise_cooldown_logged_alert(tmp_path):
    path = tmp_path / "mixed.csv"
    path.write_text(
        "date,sku,units,gross_sales,returns\n"
        "2026-10-28,SKU-OK,1,100,0\n"
        "2026-10-28,SKU-BAD,1,-10,0\n",
        encoding="utf-8",
    )
    db = connect(tmp_path / "seller.db")
    result = import_csv(db, path)
    assert (result["accepted"], result["rejected"]) == (1, 1)

    alerts = evaluate_alerts(db, Decimal(-100000), Decimal(0), dry_run=True)
    assert alerts == [f"Import batch #1 from {path.name} rejected 1 rows"]
    assert db.execute("select count(*) from alert_log").fetchone()[0] == 0

    logged = evaluate_alerts(db, Decimal(-100000), Decimal(0), dry_run=False)
    assert logged == alerts
    assert tuple(db.execute("select alert_key, mode from alert_log").fetchone()) == (
        "import_error:batch-1",
        "logged",
    )
    assert evaluate_alerts(db, Decimal(-100000), Decimal(0), dry_run=True) == []


def test_telegram_delivery_logs_without_retry_or_token_output(tmp_path):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            return b'{"ok": true}'

    db = connect(tmp_path / "seller.db")
    with patch("urllib.request.urlopen", return_value=Response()) as send:
        assert send_telegram_alerts(db, ["SKU-1: ROAS 1 below 3"], "test-secret-token", "42") == (1, 0)
    assert send.call_count == 1
    assert tuple(db.execute("select alert_key,mode from alert_log").fetchone()) == ("roas:SKU-1", "sent")


def test_telegram_delivery_requires_credentials(tmp_path):
    db = connect(tmp_path / "seller.db")
    try:
        send_telegram_alerts(db, ["SKU-1: alert"], "", "42")
    except ValueError as exc:
        assert "TELEGRAM_BOT_TOKEN" in str(exc)
    else:
        raise AssertionError("missing token must fail")


def test_cli_preview_import_dashboard_and_export_on_synthetic_fixture(tmp_path):
    project = Path(__file__).resolve().parents[1]
    database = tmp_path / "cli.sqlite3"

    def cli(*args):
        return subprocess.run(
            [sys.executable, "seller_profit_guard.py", *args],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    preview_result = json.loads(cli("preview", "fixtures/daily_sku.csv"))
    assert (preview_result["valid_rows"], preview_result["rejected_rows"]) == (3, 0)
    assert "_source_row" not in preview_result["sample"][0]
    assert "_source_row" not in preview_result["sample"][0]

    imported = json.loads(cli("--db", str(database), "import", "fixtures/daily_sku.csv"))
    assert imported == {"duplicate_batch": False, "accepted": 3, "rejected": 0, "errors": []}

    ragged = tmp_path / "ragged.csv"
    ragged.write_text(
        "date,sku,units,gross_sales\n"
        "2026-10-28,CLI-GOOD,1,100\n"
        "2026-10-28,CLI-BAD,1,50,unexpected\n",
        encoding="utf-8",
    )
    ragged_result = json.loads(cli("--db", str(tmp_path / "ragged.sqlite3"), "import", str(ragged)))
    assert ragged_result["accepted"] == 1 and ragged_result["rejected"] == 1
    assert ragged_result["errors"] == [{
        "line": 3,
        "error": "row has 1 extra field(s) beyond the 4 headers",
    }]

    dashboard_result = json.loads(cli("--db", str(database), "dashboard"))
    alpha = next(row for row in dashboard_result if row["sku"] == "SKU-ALPHA")
    assert alpha["metrics"]["contribution_margin"] == "5000"
    history = json.loads(cli("--db", str(database), "imports"))
    assert history[0]["accepted"] == 3 and history[0]["rejected"] == 0
    facts = json.loads(cli("--db", str(database), "facts"))
    assert facts["products"] == 2 and facts["order_items"] == facts["orders"] == 0
    facts = json.loads(cli("--db", str(database), "facts"))
    assert facts["products"] == 2 and facts["order_items"] == facts["orders"] == 0

    exported = tmp_path / "export.csv"
    cli("--db", str(database), "export", str(exported))
    assert exported.read_text(encoding="utf-8").count("SKU-") == 3


def test_alert_cli_applies_configured_cooldown(tmp_path):
    project = Path(__file__).resolve().parents[1]
    database = tmp_path / "cooldown.sqlite3"
    subprocess.run([sys.executable, "seller_profit_guard.py", "--db", str(database), "import", "fixtures/daily_sku.csv"],
        cwd=project, check=True, capture_output=True, text=True)
    db = connect(database)
    db.execute("INSERT INTO alert_log(alert_key,sent_at,mode) VALUES(?,?,?)",
        ("margin:SKU-ALPHA", datetime.now().astimezone().isoformat(), "sent"))
    db.commit()
    db.close()
    output = subprocess.run([sys.executable, "seller_profit_guard.py", "--db", str(database), "alerts",
        "--min-margin", "6000", "--min-stock-days", "0", "--cooldown-hours", "1000", "--trend-days", "7"],
        cwd=project, check=True, capture_output=True, text=True).stdout
    assert output.strip() == "No alerts"
