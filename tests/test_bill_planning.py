"""Bill planning: spend forecasts and unit cost, on FOCUS files with hand-worked figures."""

from __future__ import annotations

import pytest

from cloudprice_mcp import dispatch
from cloudprice_mcp.finops.bill_planning import calculate_unit_cost, forecast_spend
from cloudprice_mcp.finops.focus_ingest import BillError
from tests.test_bill_insights import COLUMNS, days, row, write


@pytest.fixture
def daily_trend(tmp_path):
    # Oct 1-30: 10, 11, ... 39 a day. A clean line: slope 1/day, no noise.
    rows = [row(d, "Amazon EC2", 10 + i) for i, d in enumerate(days(30))]
    rows.append(row("2026-10-03", "Savings Plan", 5000, ChargeCategory="Purchase"))
    return write(tmp_path / "daily.csv", rows)


def test_daily_forecast_month_end_and_following_months(daily_trend):
    r = forecast_spend(str(daily_trend), horizon_months=2)
    assert r["method"] == "daily_trend" and r["current_month_partial"]
    assert r["history_by_month"] == [{"month": "2026-10", "cost": 735.0}]  # purchase left out
    assert r["daily_run_rate"] == 39.0
    assert r["trend_per_month"] == 924.16  # 1/day/day x 30.4 x 30.4
    oct_, nov, dec = r["forecast"]
    # Oct: 735 to date + Oct 31 at 40. Nov: days at 41..70. Dec: 71..101.
    assert oct_ == {
        "month": "2026-10",
        "forecast": 775.0,
        "low": 775.0,
        "high": 775.0,
        "actual_to_date": 735.0,
    }
    assert (nov["month"], nov["forecast"]) == ("2026-11", 1665.0)
    assert (dec["month"], dec["forecast"]) == ("2026-12", 2666.0)
    assert r["headline"].startswith("Month-end 2026-10 forecast: 775.00")


def test_daily_forecast_range_widens_with_noise(tmp_path):
    rows = [row(d, "EC2", 100 + (5 if i % 2 else -5)) for i, d in enumerate(days(30))]
    nov = forecast_spend(str(write(tmp_path / "n.csv", rows)), horizon_months=1)["forecast"][1]
    assert nov["low"] < nov["forecast"] < nov["high"]
    assert nov["forecast"] == pytest.approx(3000, rel=0.02)


def test_monthly_trend_and_fastest_growing(tmp_path):
    rows = []
    for k, month in enumerate(["2026-07", "2026-08", "2026-09", "2026-10"]):
        rows.append(row(f"{month}-01", "Amazon EC2", 1000 + 100 * k))
        rows.append(row(f"{month}-01", "Amazon S3", 100, category="Storage"))
    r = forecast_spend(str(write(tmp_path / "m.csv", rows)), horizon_months=2)
    assert r["method"] == "monthly_trend" and r["trend_per_month"] == 100.0
    assert [(f["month"], f["forecast"]) for f in r["forecast"]] == [
        ("2026-11", 1500.0),
        ("2026-12", 1600.0),
    ]
    assert r["fastest_growing"] == [
        {"ServiceName": "Amazon EC2", "growth_per_month": 100.0, "latest_month": 1300.0}
    ]  # S3 is flat
    assert r["headline"] == (
        "2026-11 forecast: 1,500.00 (range 1,500.00-1,500.00). Trend: +100.00 per month. "
        "Fastest growing: Amazon EC2 (+100.00/month)."
    )


def test_short_history_falls_back_to_run_rate(tmp_path):
    rows = [row("2026-09-01", "EC2", 800), row("2026-10-01", "EC2", 900)]
    r = forecast_spend(str(write(tmp_path / "s.csv", rows)))
    assert r["method"] == "run_rate"
    assert [f["forecast"] for f in r["forecast"]] == [900.0, 900.0, 900.0]
    assert any("only 2 month(s)" in w for w in r["warnings"])
    with pytest.raises(BillError, match="horizon_months"):
        forecast_spend(str(write(tmp_path / "s2.csv", rows)), horizon_months=0)


# --- unit cost ---------------------------------------------------------------


@pytest.fixture
def product_bill(tmp_path):
    api, web = '{"product": "api"}', '{"user:Product": "web"}'
    rows = [
        row("2026-09-01", "Amazon EC2", 600, Tags=api),
        row("2026-09-01", "Amazon EC2", 400, Tags=web),
        row("2026-10-01", "Amazon EC2", 660, Tags=api),
        row("2026-10-01", "Amazon EC2", 540, Tags=web),
        row("2026-10-01", "Credit", -300, ChargeCategory="Credit"),
    ]
    return write(tmp_path / "p.csv", rows, columns=COLUMNS)


UNITS = [{"month": "2026-09", "units": 100000}, {"month": "2026-10", "units": 150000}]


def test_unit_cost_whole_bill(product_bill):
    r = calculate_unit_cost(str(product_bill), UNITS, unit_name="request")
    assert r["per_month"] == [
        {"month": "2026-09", "cost": 1000.0, "units": 100000.0, "cost_per_unit": 0.01},
        {"month": "2026-10", "cost": 1200.0, "units": 150000.0, "cost_per_unit": 0.008},
    ]  # the credit is not recurring spend
    assert r["trend"] == {
        "from": "2026-09",
        "to": "2026-10",
        "cost_change_pct": 20.0,
        "units_change_pct": 50.0,
        "cost_per_unit_change_pct": -20.0,
    }
    assert "scaling slower than the business" in r["headline"]


def test_unit_cost_scoped_to_one_product(product_bill):
    r = calculate_unit_cost(str(product_bill), UNITS, tag_key="product", tag_value="web")
    assert [m["cost"] for m in r["per_month"]] == [400.0, 540.0]  # matches user:Product
    assert r["trend"]["cost_per_unit_change_pct"] == -10.0  # 0.004 -> 0.0036
    with pytest.raises(BillError, match="together"):
        calculate_unit_cost(str(product_bill), UNITS, tag_key="product")


def test_unit_cost_input_checks(product_bill):
    for bad in ([], [{"month": "2026-13", "units": 5}], [{"month": "2026-10", "units": 0}], ["x"]):
        with pytest.raises(BillError):
            calculate_unit_cost(str(product_bill), bad)
    r = calculate_unit_cost(str(product_bill), [*UNITS, {"month": "2026-11", "units": 9}])
    assert any("2026-11" in w for w in r["warnings"])
    with pytest.raises(BillError, match="none of the months"):
        calculate_unit_cost(str(product_bill), [{"month": "2025-01", "units": 9}])


@pytest.mark.parametrize("tool", ["forecast_spend", "calculate_unit_cost"])
def test_planning_tools_are_local_only(tool, product_bill):
    args = {"file_path": str(product_bill), "units": UNITS}
    assert tool not in dispatch.tool_names()
    assert dispatch.call(tool, args) == {"error": f"Unknown tool: {tool}"}
    assert "kind" in dispatch.call(tool, args, allow_local=True)
    assert "error" in dispatch.call(tool, {}, allow_local=True)
