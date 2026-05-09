"""Tests for v0.6 finops/commitment.py — optimize_commitment orchestrator."""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops.commitment import (
    ALL_SCENARIOS,
    HORIZON_MONTHS,
    load_ri_tiers,
    optimize_commitment,
    reset_ri_tier_cache,
)
from cloudprice_mcp.inventory import ComputeItem, WorkloadInventory
from cloudprice_mcp.pricing import load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()
    reset_ri_tier_cache()


# --- Loading ri_tiers.json ---


def test_load_ri_tiers_returns_all_clouds():
    data = load_ri_tiers()
    assert set(data["rates_by_cloud"].keys()) == {"aws", "azure", "gcp", "oci"}


def test_load_ri_tiers_has_all_six_scenarios_per_cloud():
    data = load_ri_tiers()
    for cloud, rates in data["rates_by_cloud"].items():
        # Excluding "none" (not a discount) and the _notes field
        rate_keys = {k for k in rates if k != "_notes"}
        expected = {
            "1yr_no_upfront",
            "1yr_all_upfront",
            "3yr_no_upfront",
            "3yr_partial_upfront",
            "3yr_all_upfront",
        }
        assert rate_keys == expected, f"{cloud} has {rate_keys}"


def test_load_ri_tiers_is_cached():
    first = load_ri_tiers()
    second = load_ri_tiers()
    assert first is second


def test_3yr_all_upfront_is_the_deepest_discount_per_cloud():
    """Sanity check on the rate ordering — deeper commitment = bigger discount."""
    data = load_ri_tiers()
    for cloud, rates in data["rates_by_cloud"].items():
        # Skip private notes field
        if "_notes" in rates:
            rates = {k: v for k, v in rates.items() if k != "_notes"}
        assert rates["3yr_all_upfront"] >= rates["3yr_no_upfront"], cloud
        assert rates["3yr_no_upfront"] >= rates["1yr_no_upfront"], cloud


# --- Input validation ---


def test_requires_compute_items():
    catalog = load_catalog()
    inv = WorkloadInventory(source_cloud="aws")  # no compute
    with pytest.raises(ValueError, match="compute"):
        optimize_commitment(catalog, inv)


def test_unknown_cloud_rejected():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)],
    )
    with pytest.raises(ValueError, match="unknown cloud"):
        optimize_commitment(catalog, inv, cloud="ibm")


def test_unknown_scenario_rejected():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)],
    )
    with pytest.raises(ValueError, match="unknown scenario"):
        optimize_commitment(catalog, inv, scenarios=["lifetime"])


# --- Output shape ---


def test_result_has_canonical_shape():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)],
    )
    result = optimize_commitment(catalog, inv)
    assert result["kind"] == "commitment_optimization"
    assert "title" in result
    assert "headline" in result
    assert result["cloud"] == "aws"
    assert result["horizon_months"] == HORIZON_MONTHS
    assert isinstance(result["on_demand_monthly_usd"], (int, float))
    assert "scenarios" in result
    assert set(result["scenarios"].keys()) == set(ALL_SCENARIOS)
    assert "recommended" in result


def test_each_scenario_has_required_fields():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)],
    )
    result = optimize_commitment(catalog, inv)
    for name, s in result["scenarios"].items():
        for field in [
            "label",
            "monthly_usd",
            "upfront_usd",
            "term_months",
            "three_year_total_usd",
            "savings_vs_ondemand_usd",
            "savings_vs_ondemand_pct",
            "payback_months",
        ]:
            assert field in s, f"{name} missing {field}"


# --- Math correctness ---


def test_none_scenario_has_zero_savings_and_zero_upfront():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)],
    )
    result = optimize_commitment(catalog, inv)
    none = result["scenarios"]["none"]
    assert none["upfront_usd"] == 0
    assert none["savings_vs_ondemand_pct"] == 0
    assert none["payback_months"] is None
    # 3-year on-demand total = monthly × 36
    assert none["three_year_total_usd"] == round(result["on_demand_monthly_usd"] * 36, 2)


def test_3yr_all_upfront_has_zero_monthly_during_term():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)],
    )
    result = optimize_commitment(catalog, inv)
    s = result["scenarios"]["3yr_all_upfront"]
    assert s["upfront_usd"] > 0
    # All 3 years paid upfront → monthly during term should be ~0 (rounding-floor)
    assert s["monthly_usd"] < 1.0


def test_3yr_no_upfront_has_zero_upfront():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)],
    )
    result = optimize_commitment(catalog, inv)
    s = result["scenarios"]["3yr_no_upfront"]
    assert s["upfront_usd"] == 0
    assert s["monthly_usd"] > 0
    # 3yr no upfront monthly should be on-demand × (1 - discount)
    on_demand = result["on_demand_monthly_usd"]
    expected_discount = load_ri_tiers()["rates_by_cloud"]["aws"]["3yr_no_upfront"]
    expected_monthly = on_demand * (1 - expected_discount)
    assert abs(s["monthly_usd"] - expected_monthly) < 1.0


def test_3yr_partial_upfront_has_half_total_as_upfront():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)],
    )
    result = optimize_commitment(catalog, inv)
    s = result["scenarios"]["3yr_partial_upfront"]
    # The upfront should be half of the discounted 3-year cost
    on_demand = result["on_demand_monthly_usd"]
    discount = load_ri_tiers()["rates_by_cloud"]["aws"]["3yr_partial_upfront"]
    expected_total_discounted = on_demand * 36 * (1 - discount)
    expected_upfront = expected_total_discounted * 0.5
    assert abs(s["upfront_usd"] - expected_upfront) < 1.0


def test_payback_period_calculation():
    """For 3yr_partial_upfront: upfront / (on_demand_monthly - monthly_during_term)."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)],
    )
    result = optimize_commitment(catalog, inv)
    s = result["scenarios"]["3yr_partial_upfront"]
    on_demand = result["on_demand_monthly_usd"]
    monthly_savings = on_demand - s["monthly_usd"]
    expected_payback = round(s["upfront_usd"] / monthly_savings, 1)
    assert s["payback_months"] == expected_payback


def test_payback_is_zero_for_no_upfront_scenarios():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)],
    )
    result = optimize_commitment(catalog, inv)
    # No upfront cost = immediate payback
    assert result["scenarios"]["1yr_no_upfront"]["payback_months"] == 0
    assert result["scenarios"]["3yr_no_upfront"]["payback_months"] == 0


def test_savings_vs_ondemand_pct_is_consistent_with_total():
    """3yr_total reduction matches the savings_pct calculation."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)],
    )
    result = optimize_commitment(catalog, inv)
    on_demand_3yr = result["scenarios"]["none"]["three_year_total_usd"]
    for name, s in result["scenarios"].items():
        if name == "none":
            continue
        actual_savings = on_demand_3yr - s["three_year_total_usd"]
        expected_pct = round(100 * actual_savings / on_demand_3yr)
        assert s["savings_vs_ondemand_pct"] == expected_pct, (
            f"{name}: pct={s['savings_vs_ondemand_pct']} expected {expected_pct}"
        )


# --- Recommendation logic ---


def test_recommended_has_lowest_3yr_total():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)],
    )
    result = optimize_commitment(catalog, inv)
    rec = result["scenarios"][result["recommended"]]
    for s in result["scenarios"].values():
        assert rec["three_year_total_usd"] <= s["three_year_total_usd"]


def test_recommended_is_3yr_all_upfront_when_aws_aggressive():
    """AWS 3yr all upfront is the deepest discount, so it should win for AWS."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)],
    )
    result = optimize_commitment(catalog, inv)
    assert result["recommended"] == "3yr_all_upfront"


# --- Cloud override ---


def test_cloud_param_overrides_inventory_source():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)],
    )
    result = optimize_commitment(catalog, inv, cloud="azure")
    assert result["cloud"] == "azure"


def test_falls_back_to_aws_when_no_source_no_cloud():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)],
    )
    result = optimize_commitment(catalog, inv)
    assert result["cloud"] == "aws"


# --- Free tier handling ---


def test_oci_a1_workload_returns_all_free():
    """OCI A1.Flex Always Free covers up to 4 OCPU + 24 GB at $0/mo."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="oci",
        compute=[ComputeItem(name="api", vcpus=4, memory_gb=16)],  # fits A1
    )
    result = optimize_commitment(catalog, inv)
    assert result["on_demand_monthly_usd"] == 0
    for s in result["scenarios"].values():
        assert s["three_year_total_usd"] == 0
    assert result["recommended"] == "none"
    assert "free tier" in result["headline"].lower()


# --- Scenario filtering ---


def test_scenario_filter_returns_only_requested_subset():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)],
    )
    result = optimize_commitment(
        catalog, inv, scenarios=["none", "3yr_partial_upfront"]
    )
    assert set(result["scenarios"].keys()) == {"none", "3yr_partial_upfront"}


# --- Honest gaps ---


def test_honest_gaps_disclosed():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)],
    )
    result = optimize_commitment(catalog, inv)
    gaps = " ".join(result["honest_gaps"]).lower()
    assert "per-family" in gaps
    assert "renewal" in gaps  # 1yr renewal modeling caveat
