"""Bill planning — the FinOps Framework's "quantify business value" capabilities.

  forecast_spend       Forecasting: month-end and the next few months from the
                       trend in a FOCUS export, with a range, plus the services
                       growing fastest.
  calculate_unit_cost  Unit economics: cost per customer / request / order, from
                       the bill and business volumes the caller supplies, and
                       whether spend is growing faster than the business.

Same privacy rules as analyze_bill: local file, no network, local-only tools.
"""

from __future__ import annotations

import calendar
import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .bill_insights import _load, _tag, _tags
from .focus_ingest import BillError, row_cost

# One-off and corrective charges distort a trend: an upfront RI purchase is a
# spike that will not recur, a credit a dip that will not either.
_NOT_RECURRING = frozenset({"Purchase", "Credit", "Adjustment"})
_Z95 = 1.96


def _recurring(rows: list[dict[str, str]]) -> list[tuple[dict[str, str], float]]:
    out = []
    for r in rows:
        cost = row_cost(r)
        if cost is not None and (r.get("ChargeCategory") or "Usage") not in _NOT_RECURRING:
            out.append((r, cost))
    return out


def _add_months(month: str, k: int) -> str:
    y, m = int(month[:4]), int(month[5:7]) - 1 + k
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def _fit(values: list[float]) -> tuple[float, float, float]:
    """Least-squares line through values at t = 0, 1, 2...: (slope, intercept, residual sd)."""
    t = list(range(len(values)))
    fit = statistics.linear_regression(t, values)
    residuals = [v - (fit.intercept + fit.slope * i) for i, v in zip(t, values, strict=True)]
    dof = len(values) - 2
    sd = math.sqrt(sum(e * e for e in residuals) / dof) if dof > 0 else 0.0
    return fit.slope, fit.intercept, sd


# --- Forecasting -------------------------------------------------------------


def forecast_spend(file_path: str, horizon_months: int = 3) -> dict[str, Any]:
    if not 1 <= horizon_months <= 12:
        raise BillError("horizon_months must be between 1 and 12")
    rows, warnings = _load(file_path)
    costed = _recurring(rows)
    by_day: dict[str, float] = defaultdict(float)
    by_month: dict[str, float] = defaultdict(float)
    for r, cost in costed:
        start = r.get("ChargePeriodStart") or ""
        if len(start) >= 10:
            by_day[start[:10]] += cost
        if len(start) >= 7:
            by_month[start[:7]] += cost
    if not by_month:
        raise BillError("no rows with a readable ChargePeriodStart and cost")

    if len(by_day) >= 28:
        result = _forecast_daily(by_day, horizon_months)
    else:
        result = _forecast_monthly(by_month, horizon_months, warnings)

    result.update(
        {
            "kind": "spend_forecast",
            "file": Path(file_path).name,
            "history_by_month": [
                {"month": m, "cost": round(v, 2)} for m, v in sorted(by_month.items())
            ],
            "fastest_growing": _growth(costed),
            "warnings": warnings,
            "honest_gaps": [
                "A straight-line trend on recurring spend. It cannot see launches, "
                "migrations, price changes or commitments you are about to buy.",
                "Purchases (upfront commitments), credits and adjustments are left out "
                "of the trend: they are one-off and would bend it.",
                "The range is a 95% band from how far past spend strayed from the "
                "trend; it widens with fewer data points but assumes the trend holds.",
                "Daily exports give a better forecast than monthly ones: 28 days or more "
                "switches to a daily trend and a month-end estimate.",
            ],
        }
    )
    result["headline"] = _forecast_headline(result)
    return result


def _forecast_daily(by_day: dict[str, float], horizon: int) -> dict[str, Any]:
    first = date.fromisoformat(min(by_day))
    last = date.fromisoformat(max(by_day))
    start = max(first, last - timedelta(days=89))  # the last 90 days set the trend
    series = [
        by_day.get((start + timedelta(days=i)).isoformat(), 0.0)
        for i in range((last - start).days + 1)
    ]
    slope, intercept, sd = _fit(series)

    month_end = date(last.year, last.month, calendar.monthrange(last.year, last.month)[1])
    end = date.fromisoformat(_add_months(last.isoformat()[:7], horizon + 1) + "-01")
    end -= timedelta(days=1)
    months: dict[str, list[float]] = defaultdict(list)
    t = len(series) - 1
    day = last
    while day < end:
        day += timedelta(days=1)
        t += 1
        months[day.isoformat()[:7]].append(max(0.0, intercept + slope * t))

    current = last.isoformat()[:7]
    to_date = sum(v for d, v in by_day.items() if d.startswith(current))
    forecast = []
    for month, days in sorted(months.items()):
        base = to_date if month == current else 0.0
        mid = base + sum(days)
        spread = _Z95 * sd * math.sqrt(len(days))
        forecast.append(
            {
                "month": month,
                "forecast": round(mid, 2),
                "low": round(max(base, mid - spread), 2),
                "high": round(mid + spread, 2),
                **({"actual_to_date": round(to_date, 2)} if month == current else {}),
            }
        )
    return {
        "method": "daily_trend",
        "trend_days": len(series),
        # slope is per day, per day: the month's total moves by 30.4 x 30.4 of it.
        "trend_per_month": round(slope * 30.4 * 30.4, 2),
        "daily_run_rate": round(intercept + slope * (len(series) - 1), 2),
        "current_month_partial": last < month_end,
        "forecast": forecast,
    }


def _forecast_monthly(by_month: dict[str, float], horizon: int, warnings: list[str]):
    months = sorted(by_month)
    values = [by_month[m] for m in months]
    if len(values) >= 3:
        slope, intercept, sd = _fit(values)
        method = "monthly_trend"
    else:
        slope, intercept, sd = 0.0, values[-1], 0.0
        method = "run_rate"
        warnings.append(
            f"only {len(values)} month(s) of history: the forecast repeats the last month"
        )
    warnings.append(
        "a monthly export cannot show whether the last month is complete; if it is "
        "partial, the forecast will read low"
    )
    n = len(values)
    forecast = []
    for k in range(1, horizon + 1):
        mid = max(0.0, intercept + slope * (n - 1 + k))
        spread = _Z95 * sd
        forecast.append(
            {
                "month": _add_months(months[-1], k),
                "forecast": round(mid, 2),
                "low": round(max(0.0, mid - spread), 2),
                "high": round(mid + spread, 2),
            }
        )
    return {
        "method": method,
        "trend_per_month": round(slope, 2),
        "forecast": forecast,
    }


def _growth(costed: list[tuple[dict[str, str], float]]) -> list[dict[str, Any]]:
    """Services whose monthly spend is rising fastest (needs two months or more)."""
    series: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    months: set[str] = set()
    for r, cost in costed:
        month = (r.get("ChargePeriodStart") or "")[:7]
        if month:
            months.add(month)
            series[r.get("ServiceName") or "(blank)"][month] += cost
    ordered = sorted(months)
    if len(ordered) < 2:
        return []
    rising = []
    for service, points in series.items():
        values = [points.get(m, 0.0) for m in ordered]
        slope = statistics.linear_regression(range(len(values)), values).slope
        if slope > 0.005:
            rising.append(
                {
                    "ServiceName": service,
                    "growth_per_month": round(slope, 2),
                    "latest_month": round(values[-1], 2),
                }
            )
    rising.sort(key=lambda g: g["growth_per_month"], reverse=True)
    return rising[:5]


def _forecast_headline(r: dict[str, Any]) -> str:
    nxt = r["forecast"][0]
    when = f"Month-end {nxt['month']}" if "actual_to_date" in nxt else nxt["month"]
    line = f"{when} forecast: {nxt['forecast']:,.2f} (range {nxt['low']:,.2f}-{nxt['high']:,.2f})."
    trend = r["trend_per_month"]
    if r["method"] != "run_rate":
        line += f" Trend: {trend:+,.2f} per month."
    if r["fastest_growing"]:
        g = r["fastest_growing"][0]
        line += f" Fastest growing: {g['ServiceName']} ({g['growth_per_month']:+,.2f}/month)."
    return line


# --- Unit economics ----------------------------------------------------------


def calculate_unit_cost(
    file_path: str,
    units: list[dict[str, Any]],
    unit_name: str = "unit",
    tag_key: str | None = None,
    tag_value: str | None = None,
) -> dict[str, Any]:
    volumes = _volumes(units)
    if (tag_key is None) != (tag_value is None):
        raise BillError("give tag_key and tag_value together, or neither")
    rows, warnings = _load(file_path)

    cost_by_month: dict[str, float] = defaultdict(float)
    for r, cost in _recurring(rows):
        if tag_key and (_tag(_tags(r.get("Tags"))[0], tag_key) or "") != tag_value:
            continue
        month = (r.get("ChargePeriodStart") or "")[:7]
        if month:
            cost_by_month[month] += cost

    missing = sorted(m for m in volumes if m not in cost_by_month)
    if missing:
        warnings.append(f"no matching cost for month(s) {', '.join(missing)}; skipped")
    months = sorted(m for m in volumes if m in cost_by_month)
    if not months:
        raise BillError("none of the months in units appear in the bill")

    per_month = [
        {
            "month": m,
            "cost": round(cost_by_month[m], 2),
            "units": volumes[m],
            "cost_per_unit": round(cost_by_month[m] / volumes[m], 6),
        }
        for m in months
    ]
    first, last = per_month[0], per_month[-1]
    trend = None
    if len(per_month) >= 2:

        def change(a: float, b: float) -> float | None:
            return round((b - a) / a * 100, 1) if a else None

        trend = {
            "from": first["month"],
            "to": last["month"],
            "cost_change_pct": change(first["cost"], last["cost"]),
            "units_change_pct": change(first["units"], last["units"]),
            "cost_per_unit_change_pct": change(first["cost_per_unit"], last["cost_per_unit"]),
        }

    return {
        "kind": "unit_cost",
        "file": Path(file_path).name,
        "unit_name": unit_name,
        "scope": {"tag_key": tag_key, "tag_value": tag_value} if tag_key else "whole bill",
        "headline": _unit_headline(last, trend, unit_name),
        "per_month": per_month,
        "trend": trend,
        "warnings": warnings,
        "honest_gaps": [
            "Units come from the caller; the cost side is recurring spend from the "
            "bill (purchases, credits and adjustments left out).",
            "Scope with tag_key/tag_value to cost one product or team. Untagged and "
            "shared costs are then excluded, so the figure is a floor, not the full "
            "cost to serve.",
            "A falling cost per unit while units grow is the healthy pattern: spend "
            "scaling slower than the business.",
        ],
    }


def _volumes(units: list[dict[str, Any]]) -> dict[str, float]:
    if not isinstance(units, list) or not units:
        raise BillError('units must be a list like [{"month": "2026-10", "units": 120000}]')
    bad = BillError('each units entry needs "month" as YYYY-MM and "units" above 0')
    out: dict[str, float] = {}
    for u in units:
        if not isinstance(u, dict):
            raise bad
        month = str(u.get("month", ""))
        try:
            count = float(u.get("units", 0))
            date.fromisoformat(month + "-01")
        except (TypeError, ValueError):
            raise bad from None
        if len(month) != 7 or count <= 0:
            raise bad
        out[month] = out.get(month, 0.0) + count
    return out


def _unit_headline(last, trend, unit_name) -> str:
    line = f"{last['month']}: {last['cost_per_unit']:,.4f} per {unit_name}."
    if trend and trend["cost_per_unit_change_pct"] is not None:
        cpu = trend["cost_per_unit_change_pct"]
        line += (
            f" Since {trend['from']}: cost {trend['cost_change_pct']:+}%, "
            f"{unit_name}s {trend['units_change_pct']:+}%, cost per {unit_name} {cpu:+}%"
        )
        if cpu < 0:
            line += ": spend is scaling slower than the business."
        elif cpu > 0:
            line += ": spend is growing faster than the business."
        else:
            line += "."
    return line
