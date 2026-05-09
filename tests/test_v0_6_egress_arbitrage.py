"""Tests for v0.6 finops/egress_arbitrage.py — find_egress_arbitrage."""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops.egress_arbitrage import find_egress_arbitrage
from cloudprice_mcp.inventory import EgressItem, OneTime, WorkloadInventory
from cloudprice_mcp.pricing import load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


# --- Input validation ---


def test_requires_source_cloud():
    catalog = load_catalog()
    inv = WorkloadInventory(egress=[EgressItem(name="x", gb_per_month=1000)])
    with pytest.raises(ValueError, match="source_cloud"):
        find_egress_arbitrage(catalog, inv)


def test_requires_egress_items():
    catalog = load_catalog()
    inv = WorkloadInventory(source_cloud="aws")
    with pytest.raises(ValueError, match="egress must contain"):
        find_egress_arbitrage(catalog, inv)


# --- Output shape ---


def test_result_has_canonical_shape():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=10_000)],
    )
    result = find_egress_arbitrage(catalog, inv)
    assert result["kind"] == "egress_arbitrage"
    assert result["source_cloud"] == "aws"
    assert "title" in result
    assert "headline" in result
    assert "source_monthly_usd" in result
    assert "annual_source_usd" in result
    assert "one_time_exit_cost_usd" in result
    assert "targets" in result
    assert "ranking_by_3yr_savings" in result
    assert "recommended" in result


def test_default_targets_excludes_source():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=10_000)],
    )
    result = find_egress_arbitrage(catalog, inv)
    assert "aws" not in result["targets"]
    assert set(result["targets"].keys()) == {"azure", "gcp", "oci"}


def test_explicit_targets_respected():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=10_000)],
    )
    result = find_egress_arbitrage(catalog, inv, targets=["oci"])
    assert set(result["targets"].keys()) == {"oci"}


def test_per_target_has_required_fields():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=50_000)],
    )
    result = find_egress_arbitrage(catalog, inv)
    for cloud, t in result["targets"].items():
        for field in [
            "monthly_usd",
            "monthly_savings_usd",
            "annual_savings_usd",
            "savings_vs_source_pct",
            "payback_months",
            "three_year_savings_usd",
        ]:
            assert field in t, f"{cloud} missing {field}"


# --- The OCI 12× moat (the headline finding for this tool) ---


def test_oci_dominates_at_50tb_egress():
    """At 50 TB/month, OCI should be ~12× cheaper than the hyperscalers."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=50_000)],  # 50 TB
    )
    result = find_egress_arbitrage(catalog, inv)
    oci_monthly = result["targets"]["oci"]["monthly_usd"]
    aws_monthly = result["source_monthly_usd"]
    # OCI should be at least 5× cheaper (conservative — actual is ~12×)
    assert oci_monthly * 5 < aws_monthly


def test_oci_recommended_at_high_internet_egress():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=100_000)],  # 100 TB
    )
    result = find_egress_arbitrage(catalog, inv)
    assert result["recommended"] == "oci"


# --- Math ---


def test_monthly_savings_equals_source_minus_target():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=10_000)],
    )
    result = find_egress_arbitrage(catalog, inv)
    src = result["source_monthly_usd"]
    for t in result["targets"].values():
        assert t["monthly_savings_usd"] == round(src - t["monthly_usd"], 2)


def test_annual_savings_equals_monthly_x12():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=10_000)],
    )
    result = find_egress_arbitrage(catalog, inv)
    for t in result["targets"].values():
        expected = round(t["monthly_savings_usd"] * 12, 2)
        assert t["annual_savings_usd"] == expected


def test_payback_calculated_when_exit_cost_present():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=50_000)],
        one_time=OneTime(data_to_migrate_gb=20_000),
    )
    result = find_egress_arbitrage(catalog, inv)
    oci = result["targets"]["oci"]
    if oci["monthly_savings_usd"] > 0:
        # exit_cost / monthly_savings = payback months
        expected = round(
            result["one_time_exit_cost_usd"] / oci["monthly_savings_usd"], 1
        )
        assert oci["payback_months"] == expected


def test_payback_zero_when_target_cheaper_no_exit_cost():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=50_000)],
    )
    result = find_egress_arbitrage(catalog, inv)
    for t in result["targets"].values():
        if t["monthly_savings_usd"] > 0:
            assert t["payback_months"] == 0


def test_payback_none_when_target_more_expensive():
    """If a target has no savings, payback is null (never pays back)."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=10_000)],
    )
    result = find_egress_arbitrage(catalog, inv)
    for t in result["targets"].values():
        if t["monthly_savings_usd"] <= 0:
            assert t["payback_months"] is None


def test_three_year_savings_includes_exit_cost():
    """3yr savings = annual × 3 - exit_cost."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=50_000)],
        one_time=OneTime(data_to_migrate_gb=20_000),
    )
    result = find_egress_arbitrage(catalog, inv)
    exit_cost = result["one_time_exit_cost_usd"]
    for t in result["targets"].values():
        expected = round(t["annual_savings_usd"] * 3 - exit_cost, 2)
        assert t["three_year_savings_usd"] == expected


# --- Ranking + recommendation ---


def test_ranking_orders_by_3yr_savings_descending():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=50_000)],
    )
    result = find_egress_arbitrage(catalog, inv)
    ranking = result["ranking_by_3yr_savings"]
    savings = [result["targets"][c]["three_year_savings_usd"] for c in ranking]
    assert savings == sorted(savings, reverse=True)


def test_targets_with_no_savings_excluded_from_ranking():
    """Only targets with positive savings should be in ranking."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="oci",  # OCI is cheapest, so other targets should have negative savings
        egress=[EgressItem(name="cdn", gb_per_month=50_000)],
    )
    result = find_egress_arbitrage(catalog, inv)
    # All other clouds are more expensive than OCI, so ranking should be empty
    assert result["ranking_by_3yr_savings"] == []
    assert result["recommended"] is None


# --- Inter-region egress ---


def test_inter_region_egress_handled():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[
            EgressItem(name="repl", gb_per_month=10_000, direction="inter_region"),
        ],
    )
    result = find_egress_arbitrage(catalog, inv)
    # Source AWS inter-region: $0.02/GB × 10,000 = $200
    # Target OCI inter-region: $0.0085/GB × 10,000 = $85 → savings = $115/mo
    oci = result["targets"]["oci"]
    assert oci["monthly_savings_usd"] > 0


def test_mix_of_internet_and_inter_region_egress():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[
            EgressItem(name="cdn", gb_per_month=50_000, direction="out_to_internet"),
            EgressItem(name="repl", gb_per_month=5_000, direction="inter_region"),
        ],
    )
    result = find_egress_arbitrage(catalog, inv)
    # Both directions contribute; total source cost should reflect both
    assert result["source_monthly_usd"] > 4000  # 50 TB internet alone is ~$4K on AWS


# --- Honest gaps ---


def test_honest_gaps_disclosed():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=10_000)],
    )
    result = find_egress_arbitrage(catalog, inv)
    gaps = " ".join(result["honest_gaps"]).lower()
    assert "vpc peering" in gaps or "direct connect" in gaps or "cdn" in gaps
