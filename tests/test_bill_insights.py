"""Bill insights: commitment gaps, bill anomalies and cost allocation on FOCUS files.

Every figure below is worked out by hand from the synthetic bills built here.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta

import pytest

from cloudprice_mcp import dispatch
from cloudprice_mcp.finops.bill_insights import (
    analyze_cost_allocation,
    detect_bill_anomalies,
    find_commitment_gaps,
)
from cloudprice_mcp.finops.focus_ingest import BillError

COLUMNS = [
    "ChargePeriodStart", "ChargeCategory", "ProviderName", "ServiceName",
    "ServiceCategory", "PricingCategory", "BilledCost", "EffectiveCost",
    "CommitmentDiscountId", "CommitmentDiscountStatus", "ResourceId",
    "SubAccountName", "Tags",
]  # fmt: skip


def row(period, service, cost, category="Compute", **extra):
    r = {
        "ChargePeriodStart": period,
        "ChargeCategory": "Usage",
        "ProviderName": "AWS",
        "ServiceName": service,
        "ServiceCategory": category,
        "PricingCategory": "Standard",
        "BilledCost": cost,
        "EffectiveCost": cost,
    }
    r.update(extra)
    return r


def write(path, rows, columns=COLUMNS):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def days(n, start=date(2026, 10, 1)):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


# --- find_commitment_gaps ----------------------------------------------------


@pytest.fixture
def daily_bill(tmp_path):
    rows = []
    for d in days(20):
        rows += [
            row(d, "Amazon EC2", 48),  # steady on-demand: 2.00/hour
            row(d, "Amazon EC2", 24, CommitmentDiscountId="sp-1"),
            row(d, "Amazon EC2", 10, PricingCategory="Dynamic"),  # spot
            row(d, "Amazon S3", 5, category="Storage"),  # not eligible
        ]
    rows += [
        row("2026-10-05", "Amazon EC2", 20, CommitmentDiscountId="sp-1",
            CommitmentDiscountStatus="Unused"),
        row("2026-10-05", "Tax", 7, ChargeCategory="Tax"),
        row("2026-10-05", "Acme VMs", 1, ProviderName="Acme Cloud"),
    ]  # fmt: skip
    return write(tmp_path / "daily.csv", rows)


def test_commitment_coverage_utilisation_and_spot(daily_bill):
    r = find_commitment_gaps(str(daily_bill))
    assert r["granularity"] == "day"
    assert r["usage_by_pricing"] == {
        "committed": 480.0,
        "unused_commitment": 20.0,
        "spot": 200.0,
        "on_demand": 1061.0,  # 960 EC2 + 100 S3 + 1 Acme
    }
    # Eligible: 480 committed + 961 on-demand compute (S3 is storage, spot excluded).
    assert r["coverage"] == {"committed": 480.0, "eligible_on_demand": 961.0, "coverage_pct": 33.3}
    assert r["utilization"] == {"used": 480.0, "unused": 20.0, "utilization_pct": 96.0}
    assert any("Acme Cloud" in w for w in r["warnings"])


def test_commitment_recommendation_is_sized_on_the_steady_floor(daily_bill):
    (rec,) = find_commitment_gaps(str(daily_bill))["recommendations"]
    assert rec["cloud"] == "aws"
    assert rec["steady_on_demand_per_hour"] == 2.0
    one, three = rec["options"]
    # AWS 1yr no-upfront discount 0.32: commit 2 x 0.68, save 2 x 0.32 x 8760.
    assert one == {
        "scenario": "1yr_no_upfront",
        "label": "1-year, no upfront",
        "discount": 0.32,
        "commitment_per_hour": 1.36,
        "annual_saving": 5606.4,
    }
    assert three["annual_saving"] == 8409.6  # 2 x 0.48 x 8760


def test_commitment_monthly_uses_lowest_month_and_counts_empty_months(tmp_path):
    months = [
        row("2026-07-01", "Amazon EC2", 730),
        row("2026-08-01", "Amazon EC2", 1460),
        row("2026-09-01", "Amazon RDS", 1095, category="Databases"),
    ]
    rec = find_commitment_gaps(str(write(tmp_path / "m.csv", months)))["recommendations"][0]
    assert rec["steady_on_demand_per_hour"] == 1.0  # 730 / 730 hours
    assert rec["basis"] == "lowest of 3 monthly totals"

    # A month fully covered by commitments has zero on-demand: commit nothing more.
    covered = [
        row("2026-08-01", "Amazon EC2", 500, CommitmentDiscountId="ri-1"),
        row("2026-09-01", "Amazon EC2", 1460),
    ]
    r = find_commitment_gaps(str(write(tmp_path / "c.csv", covered)))
    assert r["recommendations"] == []
    assert "No commitment" not in r["headline"]


# --- detect_bill_anomalies ---------------------------------------------------


@pytest.fixture
def noisy_bill(tmp_path):
    rows = []
    for i, d in enumerate(days(21)):
        rows.append(row(d, "Amazon EC2", 400 if i == 18 else (98 if i % 2 else 102)))
        rows.append(row(d, "Amazon S3", 10, category="Storage"))
        if i not in (15, 16):  # two missing days read as zero spend
            rows.append(row(d, "AWS Lambda", 30))
        rows.append(row(d, "CloudWatch", 5 if i == 10 else 1))  # below min_impact
    rows.append(row(days(21)[-1], "Amazon Bedrock", 50, category="AI and Machine Learning"))
    return write(tmp_path / "noisy.csv", rows)


def test_anomalies_spike_drop_and_new_spend(noisy_bill):
    r = detect_bill_anomalies(str(noisy_bill))
    assert r["granularity"] == "day" and r["periods"] == 21
    spike, drop, new = r["anomalies"]
    assert spike["group"] == "Amazon EC2" and spike["type"] == "spike"
    assert spike["start"] == "2026-10-19" and spike["excess_cost"] == 300.0
    assert spike["normal_cost"] == 100.0 and not spike["ongoing"]
    # Two zero days merge into one event; the peak of a drop is its low point.
    assert drop["group"] == "AWS Lambda" and drop["type"] == "drop"
    assert (drop["start"], drop["end"], drop["periods"]) == ("2026-10-16", "2026-10-17", 2)
    assert drop["excess_cost"] == -60.0 and drop["peak_cost"] == 0.0
    assert new["group"] == "Amazon Bedrock" and new["type"] == "new_spend" and new["ongoing"]
    assert "Amazon EC2 spike 2026-10-19" in r["headline"]
    assert r["anomaly_count"] == 3  # CloudWatch's +4 is under min_impact


def test_anomalies_need_history_and_validate_input(tmp_path, noisy_bill):
    short = write(tmp_path / "s.csv", [row("2026-09-01", "EC2", 1), row("2026-10-01", "EC2", 9)])
    r = detect_bill_anomalies(str(short))
    assert r["anomalies"] == [] and "Not enough history" in r["headline"]
    with pytest.raises(BillError, match="sensitivity"):
        detect_bill_anomalies(str(noisy_bill), sensitivity="loud")
    with pytest.raises(BillError, match="group_by"):
        detect_bill_anomalies(str(noisy_bill), group_by="Tags")
    # Strict needs a bigger move than moderate: a 25% jump passes moderate only.
    rows = [row(d, "EC2", 100) for d in days(15)] + [row("2026-10-16", "EC2", 125)]
    bump = write(tmp_path / "b.csv", rows)
    assert detect_bill_anomalies(str(bump))["anomaly_count"] == 1
    assert detect_bill_anomalies(str(bump), sensitivity="strict")["anomaly_count"] == 0


# --- analyze_cost_allocation -------------------------------------------------


@pytest.fixture
def tagged_bill(tmp_path):
    d = "2026-10-01"
    rows = [
        row(d, "Amazon EC2", 100, ResourceId="i-1", SubAccountName="A",
            Tags='{"user:Team": "web", "env": "prod"}'),
        row(d, "Amazon EC2", 50, ResourceId="i-2", SubAccountName="A", Tags='{"team": "data"}'),
        row(d, "Amazon S3", 30, ResourceId="b-1", SubAccountName="B", Tags=""),
        row(d, "Amazon RDS", 20, ResourceId="db-1", Tags='{"Team": "", "env": "dev"}'),
        row(d, "AWS Lambda", 5, ResourceId="fn-1", Tags="not json"),
        row(d, "Tax", 10, ChargeCategory="Tax"),
    ]  # fmt: skip
    return write(tmp_path / "tags.csv", rows)


def test_allocation_with_required_tags(tagged_bill):
    r = analyze_cost_allocation(str(tagged_bill), required_tags=["team", "env"], tag_key="team")
    assert (r["allocatable_cost"], r["unallocatable_cost"]) == (205.0, 10.0)
    assert (r["allocated_cost"], r["allocated_pct"]) == (100.0, 48.8)
    assert (r["tagged_cost"], r["tagged_pct"]) == (170.0, 82.9)  # an empty value is no tag
    assert r["per_required_tag"] == [
        {"tag": "team", "cost": 150.0, "coverage_pct": 73.2},
        {"tag": "env", "cost": 120.0, "coverage_pct": 58.5},
    ]
    assert [s["ServiceName"] for s in r["top_unallocated_services"]] == [
        "Amazon EC2", "Amazon S3", "Amazon RDS", "AWS Lambda",
    ]  # fmt: skip
    assert r["by_tag_value"][:3] == [
        {"team": "web", "cost": 100.0, "share_pct": 48.8},
        {"team": "(not tagged)", "cost": 55.0, "share_pct": 26.8},
        {"team": "data", "cost": 50.0, "share_pct": 24.4},
    ]
    assert r["by_sub_account"] == [
        {"SubAccount": "A", "cost": 150.0, "share_pct": 73.2},
        {"SubAccount": "B", "cost": 30.0, "share_pct": 14.6},
    ]
    assert any("not a JSON object" in w for w in r["warnings"])
    assert r["headline"].startswith("48.8% of allocatable cost carries all required tags")


def test_allocation_without_required_tags_or_tags_column(tmp_path, tagged_bill):
    r = analyze_cost_allocation(str(tagged_bill), required_tags="team")  # a bare string is fine
    assert r["required_tags"] == ["team"]
    assert analyze_cost_allocation(str(tagged_bill))["allocated_pct"] == 82.9  # any tag counts
    cols = [c for c in COLUMNS if c != "Tags"]
    bare = write(tmp_path / "bare.csv", [row("2026-10-01", "EC2", 5)], columns=cols)
    r = analyze_cost_allocation(str(bare))
    assert "No Tags column" in r["headline"]


# --- safety -----------------------------------------------------------------


@pytest.mark.parametrize(
    "tool", ["find_commitment_gaps", "detect_bill_anomalies", "analyze_cost_allocation"]
)
def test_bill_tools_are_local_only(tool, daily_bill):
    assert tool not in dispatch.tool_names()
    assert dispatch.call(tool, {"file_path": str(daily_bill)}) == {"error": f"Unknown tool: {tool}"}
    assert "error" in dispatch.call(tool, {}, allow_local=True)
    assert "kind" in dispatch.call(tool, {"file_path": str(daily_bill)}, allow_local=True)
    bad = dispatch.call(tool, {"file_path": str(daily_bill) + ".txt"}, allow_local=True)
    assert "only .csv" in bad["error"]
