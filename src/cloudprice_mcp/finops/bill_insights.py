"""Bill insights — FinOps Framework capabilities run on a FOCUS export.

analyze_bill (focus_ingest.py) answers "where does the money go". These answer
the next three questions a FinOps practitioner asks of the same file:

  find_commitment_gaps     Rate Optimization: how much steady on-demand spend
                           could move to Savings Plans / RIs / CUDs, what that
                           would save, and what idle commitments cost today.
  detect_bill_anomalies    Anomaly Management: cost spikes, drops and new spend
                           per service, against that service's own recent normal.
  analyze_cost_allocation  Allocation: how much spend carries the tags you charge
                           back on, and the biggest costs that carry none.

Same privacy rules as analyze_bill: the file is read locally, nothing is sent
anywhere, and all three are local-only tools (see dispatch.LOCAL_ONLY_TOOLS).
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from ..pricing import HOURS_PER_MONTH
from .commitment import load_ri_tiers
from .focus_ingest import _GROUPS, BillError, read_focus, row_cost, usage_kind

Sensitivity = Literal["strict", "moderate", "permissive"]

# FOCUS ProviderName -> the cloud keys used in data/ri_tiers.json.
_CLOUD_OF = {
    "aws": "aws",
    "amazon web services": "aws",
    "microsoft": "azure",
    "microsoft azure": "azure",
    "azure": "azure",
    "google cloud": "gcp",
    "google cloud platform": "gcp",
    "gcp": "gcp",
    "oracle": "oci",
    "oracle cloud": "oci",
    "oracle cloud infrastructure": "oci",
    "oci": "oci",
}
# FOCUS ServiceCategory values that Savings Plans / RIs / CUDs can cover.
COMMITMENT_ELIGIBLE = frozenset({"Compute", "Databases"})
_SCENARIOS = ("1yr_no_upfront", "3yr_no_upfront")

# (robust z, % change from normal). Daily cloud spend is far noisier than list
# prices, so these sit well above the price-anomaly presets in anomaly.py.
_BILL_THRESHOLDS: dict[Sensitivity, tuple[float, float]] = {
    "strict": (4.0, 30.0),
    "moderate": (3.0, 20.0),
    "permissive": (2.0, 10.0),
}
# Charges nobody can tag: they are reported apart from allocation coverage.
_UNALLOCATABLE = frozenset({"Tax", "Credit", "Adjustment"})


def _load(file_path: str) -> tuple[list[dict[str, str]], list[str]]:
    rows, warnings = read_focus(file_path)
    if not rows:
        raise BillError("the export has a header but no rows")
    return rows, warnings


def _periods(rows: list[dict[str, str]]) -> tuple[str, int]:
    """'day' when the export has two weeks or more of daily rows, else 'month'."""
    days = {(r.get("ChargePeriodStart") or "")[:10] for r in rows} - {""}
    return ("day", 10) if len(days) >= 14 else ("month", 7)


# --- Rate Optimization -------------------------------------------------------


def find_commitment_gaps(file_path: str) -> dict[str, Any]:
    rows, warnings = _load(file_path)
    if not any(r.get("ServiceCategory") for r in rows):
        warnings.append("no ServiceCategory values, so no spend can be judged commitment-eligible")
    unit, width = _periods(rows)
    all_periods = {(r.get("ChargePeriodStart") or "")[:width] for r in rows} - {""}

    paid = dict.fromkeys(("committed", "unused_commitment", "spot", "on_demand"), 0.0)
    eligible_on_demand = 0.0
    by_service: dict[str, float] = defaultdict(float)
    by_cloud: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unknown: set[str] = set()

    for r in rows:
        kind, cost = usage_kind(r), row_cost(r)
        if kind is None or cost is None:
            continue
        paid[kind] += cost
        if kind != "on_demand" or r.get("ServiceCategory") not in COMMITMENT_ELIGIBLE:
            continue
        eligible_on_demand += cost
        by_service[r.get("ServiceName") or "(blank)"] += cost
        cloud = _CLOUD_OF.get((r.get("ProviderName") or "").strip().lower())
        if cloud is None:
            unknown.add(r.get("ProviderName") or "(blank)")
            continue
        by_cloud[cloud][(r.get("ChargePeriodStart") or "")[:width]] += cost

    if unknown:
        warnings.append(
            f"no commitment rates for provider(s) {', '.join(sorted(unknown))}; "
            "their spend counts toward coverage but gets no recommendation"
        )

    rates = load_ri_tiers()
    recommendations = []
    for cloud, series in sorted(by_cloud.items()):
        # Periods with no on-demand spend count as zero: a commitment sized on
        # busy periods only would sit idle on the quiet ones.
        values = sorted(series.get(p, 0.0) for p in all_periods)
        if unit == "day":
            steady = statistics.quantiles(values, n=10, method="inclusive")[0] / 24
            basis = f"10th percentile of {len(values)} daily totals"
        else:
            steady = values[0] / HOURS_PER_MONTH
            basis = f"lowest of {len(values)} monthly totals"
        if steady <= 0:
            continue
        options = []
        for sc in _SCENARIOS:
            discount = rates["rates_by_cloud"][cloud][sc]
            options.append(
                {
                    "scenario": sc,
                    "label": rates["scenarios"][sc]["label"],
                    "discount": discount,
                    "commitment_per_hour": round(steady * (1 - discount), 4),
                    "annual_saving": round(steady * discount * HOURS_PER_MONTH * 12, 2),
                }
            )
        recommendations.append(
            {
                "cloud": cloud,
                "steady_on_demand_per_hour": round(steady, 4),
                "basis": basis,
                "options": options,
            }
        )

    eligible = paid["committed"] + eligible_on_demand
    coverage = (
        {
            "committed": round(paid["committed"], 2),
            "eligible_on_demand": round(eligible_on_demand, 2),
            "coverage_pct": round(paid["committed"] / eligible * 100, 1),
        }
        if eligible
        else None
    )
    commitments = paid["committed"] + paid["unused_commitment"]
    utilization = (
        {
            "used": round(paid["committed"], 2),
            "unused": round(paid["unused_commitment"], 2),
            "utilization_pct": round(paid["committed"] / commitments * 100, 1),
        }
        if commitments
        else None
    )
    ranked = sorted(by_service.items(), key=lambda kv: kv[1], reverse=True)

    return {
        "kind": "commitment_gaps",
        "file": Path(file_path).name,
        "granularity": unit,
        "headline": _gaps_headline(coverage, utilization, recommendations),
        "usage_by_pricing": {k: round(v, 2) for k, v in paid.items()},
        "coverage": coverage,
        "utilization": utilization,
        "recommendations": recommendations,
        "top_on_demand_eligible": [{"ServiceName": k, "cost": round(v, 2)} for k, v in ranked[:10]],
        "warnings": warnings,
        "honest_gaps": [
            "Eligible means ServiceCategory Compute or Databases on on-demand pricing. "
            "Spot is excluded: it is already discounted and cannot be committed.",
            "The recommended commitment is sized on the steady floor of your spend "
            "(the 10th-percentile day, or the lowest month), so it stays used on quiet "
            "days. Committing to the average would leave hours idle.",
            "Discounts are conservative cloud-wide averages from ri_tiers.json, not "
            "your instance families or negotiated rates. Check the cloud's own "
            "recommendation (Cost Explorer, Azure Advisor, GCP Recommender) before buying.",
            "Savings Plans and flexible CUDs commit to $/hour of discounted spend, which "
            "is what commitment_per_hour shows. RIs commit to specific instances; size "
            "those from the instance list instead.",
            "Coverage and utilisation use EffectiveCost. Without it, covered usage often "
            "bills as 0 and coverage reads too low.",
        ],
    }


def _gaps_headline(coverage, utilization, recommendations) -> str:
    parts = []
    if coverage:
        parts.append(f"Commitment coverage: {coverage['coverage_pct']}% of eligible usage.")
    else:
        parts.append("No commitment-eligible usage found.")
    if recommendations:
        hourly = sum(r["steady_on_demand_per_hour"] for r in recommendations)
        one = sum(r["options"][0]["annual_saving"] for r in recommendations)
        three = sum(r["options"][1]["annual_saving"] for r in recommendations)
        parts.append(
            f"About {hourly:,.2f}/hour of steady on-demand spend could be committed: "
            f"~{one:,.0f}/year saved on 1-year no-upfront, ~{three:,.0f}/year on 3-year."
        )
    if utilization and utilization["unused"] > 0:
        parts.append(
            f"Existing commitments are {utilization['utilization_pct']}% used; "
            f"{utilization['unused']:,.2f} went unused in this export."
        )
    return " ".join(parts)


# --- Anomaly Management ------------------------------------------------------


def detect_bill_anomalies(
    file_path: str,
    group_by: str = "ServiceName",
    sensitivity: Sensitivity = "moderate",
    min_impact: float = 10.0,
    limit: int = 10,
) -> dict[str, Any]:
    if group_by not in _GROUPS:
        raise BillError(f"group_by must be one of {', '.join(_GROUPS)}")
    if sensitivity not in _BILL_THRESHOLDS:
        raise BillError(f"sensitivity must be one of {', '.join(_BILL_THRESHOLDS)}")
    if min_impact < 0 or limit < 1:
        raise BillError("min_impact cannot be negative and limit must be at least 1")
    rows, warnings = _load(file_path)
    unit, width = _periods(rows)
    window, min_history = (14, 7) if unit == "day" else (6, 3)
    z_limit, pct_limit = _BILL_THRESHOLDS[sensitivity]

    series: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    periods: set[str] = set()
    for r in rows:
        period = (r.get("ChargePeriodStart") or "")[:width]
        cost = row_cost(r)
        if not period or cost is None or usage_kind(r) is None:
            continue
        periods.add(period)
        series[r.get(group_by) or "(blank)"][period] += cost
    timeline = sorted(periods)

    events: list[dict[str, Any]] = []
    for key, points in series.items():
        values = [points.get(p, 0.0) for p in timeline]
        events.extend(
            _events(key, timeline, values, window, min_history, z_limit, pct_limit, min_impact)
        )
    events.sort(key=lambda e: abs(e["excess_cost"]), reverse=True)

    enough = len(timeline) > min_history
    if not enough:
        warnings.append(
            f"only {len(timeline)} {unit}(s) in the export; anomaly detection needs "
            f"more than {min_history} to learn what normal looks like"
        )
    return {
        "kind": "bill_anomalies",
        "file": Path(file_path).name,
        "granularity": unit,
        "periods": len(timeline),
        "window": {"from": timeline[0], "to": timeline[-1]} if timeline else None,
        "group_by": group_by,
        "sensitivity": sensitivity,
        "thresholds": {"robust_z": z_limit, "pct_change": pct_limit, "min_impact": min_impact},
        "headline": _anomaly_headline(events, group_by, unit, enough),
        "anomaly_count": len(events),
        "anomalies": events[:limit],
        "warnings": warnings,
        "honest_gaps": [
            f"Each {unit} is compared with the median of the previous {window} {unit}s "
            "for the same group; the spread is the median absolute deviation, so one "
            "earlier spike does not hide the next.",
            "Back-to-back flagged periods are merged into one event; excess_cost is the "
            "total above (or below) normal across the event.",
            "Only Usage charges are scanned. Purchases, tax and credits are left out.",
            "A partial last period (an export taken mid-day or mid-month) looks like a "
            "drop. Check 'ongoing' events against when the export was taken.",
            "Statistics flag the unusual, not the wrong: a planned launch is a spike too.",
        ],
    }


def _events(key, timeline, values, window, min_history, z_limit, pct_limit, min_impact):
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for i in range(min_history, len(values)):
        history = values[max(0, i - window) : i]
        normal = statistics.median(history)
        spread = 1.4826 * statistics.median(abs(h - normal) for h in history)
        impact = values[i] - normal
        flagged = False
        if abs(impact) >= max(min_impact, 0.01):
            z = impact / spread if spread else float("inf")
            pct = impact / normal * 100 if normal else float("inf")
            flagged = abs(z) >= z_limit and abs(pct) >= pct_limit
        if not flagged:
            current = None
            continue
        direction = "up" if impact > 0 else "down"
        if current is not None and current["direction"] == direction:
            current["end"] = timeline[i]
            current["periods"] += 1
            current["excess_cost"] = round(current["excess_cost"] + impact, 2)
            # The peak is the most extreme point in the event's direction.
            if (values[i] > current["peak_cost"]) == (direction == "up"):
                current["peak_cost"], current["peak_period"] = round(values[i], 2), timeline[i]
        else:
            current = {
                "group": key,
                "type": "new_spend" if normal == 0 else ("spike" if impact > 0 else "drop"),
                "direction": direction,
                "start": timeline[i],
                "end": timeline[i],
                "periods": 1,
                "normal_cost": round(normal, 2),
                "peak_cost": round(values[i], 2),
                "peak_period": timeline[i],
                "excess_cost": round(impact, 2),
            }
            events.append(current)
    for e in events:
        e["ongoing"] = e["end"] == timeline[-1]
    return events


def _anomaly_headline(events, group_by, unit, enough) -> str:
    if not enough:
        return f"Not enough history to judge what is normal; export more {unit}s."
    if not events:
        return f"No unusual {group_by} spend found."
    e = events[0]
    when = e["start"] if e["start"] == e["end"] else f"{e['start']} to {e['end']}"
    what = {"new_spend": "new spend", "spike": "spike", "drop": "drop"}[e["type"]]
    line = (
        f"{len(events)} anomal{'y' if len(events) == 1 else 'ies'}. Biggest: {e['group']} "
        f"{what} {when}, {e['excess_cost']:+,.2f} against a normal {e['normal_cost']:,.2f} "
        f"per {unit}"
    )
    return line + (" (still going in the latest period)." if e["ongoing"] else ".")


# --- Allocation --------------------------------------------------------------


def analyze_cost_allocation(
    file_path: str,
    required_tags: list[str] | None = None,
    tag_key: str | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    if top_n < 1:
        raise BillError("top_n must be at least 1")
    if isinstance(required_tags, str):
        required_tags = [required_tags]
    required = [t.strip() for t in required_tags or [] if t and t.strip()]
    rows, warnings = _load(file_path)
    has_tags = "Tags" in rows[0]
    if not has_tags:
        warnings.append("the export has no Tags column, so tag coverage cannot be measured")

    allocatable = unallocatable = any_tag = allocated = 0.0
    per_tag: dict[str, float] = dict.fromkeys(required, 0.0)
    by_value: dict[str, float] = defaultdict(float)
    gap_service: dict[str, float] = defaultdict(float)
    gap_resource: dict[str, float] = defaultdict(float)
    by_account: dict[str, float] = defaultdict(float)
    unreadable = 0

    for r in rows:
        cost = row_cost(r)
        if cost is None:
            continue
        if (r.get("ChargeCategory") or "Usage") in _UNALLOCATABLE:
            unallocatable += cost
            continue
        allocatable += cost
        account = r.get("SubAccountName") or r.get("SubAccountId")
        if account:
            by_account[account] += cost
        tags, ok = _tags(r.get("Tags"))
        unreadable += not ok
        if tags:
            any_tag += cost
        present = [t for t in required if _tag(tags, t)]
        for t in present:
            per_tag[t] += cost
        complete = len(present) == len(required) if required else bool(tags)
        if complete:
            allocated += cost
        else:
            gap_service[r.get("ServiceName") or "(blank)"] += cost
            if r.get("ResourceId"):
                gap_resource[r["ResourceId"]] += cost
        if tag_key:
            by_value[_tag(tags, tag_key) or "(not tagged)"] += cost

    if unreadable:
        warnings.append(f"{unreadable} rows had a Tags value that is not a JSON object")

    def pct(part: float) -> float:
        return round(part / allocatable * 100, 1) if allocatable else 0.0

    def top(d: dict[str, float], label: str) -> list[dict[str, Any]]:
        ranked = sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        return [{label: k, "cost": round(v, 2), "share_pct": pct(v)} for k, v in ranked]

    result = {
        "kind": "cost_allocation",
        "file": Path(file_path).name,
        "required_tags": required,
        "allocatable_cost": round(allocatable, 2),
        "unallocatable_cost": round(unallocatable, 2),
        "tagged_cost": round(any_tag, 2),
        "tagged_pct": pct(any_tag),
        "allocated_cost": round(allocated, 2),
        "allocated_pct": pct(allocated),
        "per_required_tag": [
            {"tag": t, "cost": round(v, 2), "coverage_pct": pct(v)} for t, v in per_tag.items()
        ],
        "top_unallocated_services": top(gap_service, "ServiceName"),
        "top_unallocated_resources": top(gap_resource, "ResourceId"),
        "by_sub_account": top(by_account, "SubAccount") if by_account else None,
        "by_tag_value": top(by_value, tag_key) if tag_key else None,
        "warnings": warnings,
        "honest_gaps": [
            "Allocated means the row carries every required tag (or any tag at all when "
            "none are required). A tag with an empty value counts as missing.",
            "Tag keys match case-insensitively and ignore a provider prefix, so 'team' "
            "matches 'user:Team'.",
            "Tax, credits and adjustments cannot be tagged; they are reported as "
            "unallocatable and left out of the percentages.",
            "Shared costs (support, networking, shared clusters) usually need a split "
            "rule rather than a tag; this shows them as unallocated.",
            "Some exports only carry tags that were activated for cost allocation "
            "before the billing period (AWS cost allocation tags, for example).",
        ],
    }
    result["headline"] = _allocation_headline(result, required, has_tags)
    return result


def _tags(raw: str | None) -> tuple[dict[str, str], bool]:
    """Parsed Tags column, and whether it was readable. Empty means no tags."""
    if not raw or not raw.strip():
        return {}, True
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}, False
    if not isinstance(parsed, dict):
        return {}, False
    return {str(k): str(v) for k, v in parsed.items() if v not in (None, "")}, True


def _tag(tags: dict[str, str], key: str) -> str | None:
    want = key.lower()
    for k, v in tags.items():
        k = k.lower()
        if k == want or k.endswith(":" + want):
            return v
    return None


def _allocation_headline(result, required, has_tags) -> str:
    if not has_tags:
        return "No Tags column in this export; only sub-account allocation is shown."
    what = f"all required tags ({', '.join(required)})" if required else "at least one tag"
    line = (
        f"{result['allocated_pct']}% of allocatable cost carries {what}; "
        f"{result['allocatable_cost'] - result['allocated_cost']:,.2f} is unallocated."
    )
    gaps = result["top_unallocated_services"]
    if gaps:
        line += f" Biggest gap: {gaps[0]['ServiceName']} at {gaps[0]['cost']:,.2f}."
    return line
