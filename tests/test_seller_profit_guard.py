import csv
import json
import os
import stat
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from seller_profit_guard import (
    calculate,
    connect,
    evaluate_alerts,
    export_csv,
    import_csv,
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
    sheet.append(["date", "sku", "units", "gross_sales"])
    sheet.append([date(2026, 10, 28), "SKU-X", 2, 1200])
    workbook.save(path)
    workbook.close()
    rows, errors = parse_xlsx(path)
    assert not errors and rows[0]["date"] == "2026-10-28" and rows[0]["sku"] == "SKU-X"


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

    imported = json.loads(cli("--db", str(database), "import", "fixtures/daily_sku.csv"))
    assert imported == {"duplicate_batch": False, "accepted": 3, "rejected": 0}

    dashboard_result = json.loads(cli("--db", str(database), "dashboard"))
    alpha = next(row for row in dashboard_result if row["sku"] == "SKU-ALPHA")
    assert alpha["metrics"]["contribution_margin"] == "5000"

    exported = tmp_path / "export.csv"
    cli("--db", str(database), "export", str(exported))
    assert exported.read_text(encoding="utf-8").count("SKU-") == 3
