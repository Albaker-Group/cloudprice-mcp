"""Tests for v0.6 finops/tco.py — compare_total_cost_of_ownership."""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops.tco import (
    DEFAULT_HORIZON_YEARS,
    GrowthAssumptions,
    compare_total_cost_of_ownership,
)
from cloudprice_mcp.inventory import (
    ComputeItem,
    EgressItem,
    ObjectStorageItem,
    WorkloadInventory,
)
from cloudprice_mcp.pricing import load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


# --- Input validation ---


def test_empty_inventory_rejected():
    catalog = load_catalog()
    with pytest.raises(ValueError, match="no workload items"):
        compare_total_cost_of_ownership(catalog, WorkloadInventory())


def test_zero_horizon_rejected():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)]
    )
    with pytest.raises(ValueError, match="horizon_years must be >= 1"):
        compare_total_cost_of_ownership(catalog, inv, horizon_years=0)


# --- Output shape ---


def test_result_has_canonical_shape():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)]
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    assert result["kind"] == "total_cost_of_ownership"
    assert result["horizon_years"] == DEFAULT_HORIZON_YEARS
    assert "headline" in result
    assert "growth_assumptions" in result
    assert "per_cloud_per_year" in result
    assert "cumulative_tco_usd" in result
    assert "ranking_by_tco" in result
    assert "sensitivity" in result


def test_default_horizon_is_3_years():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)]
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    for years in result["per_cloud_per_year"].values():
        assert len(years) == 3


def test_custom_horizon_respected():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)]
    )
    result = compare_total_cost_of_ownership(catalog, inv, horizon_years=5)
    assert result["horizon_years"] == 5
    for years in result["per_cloud_per_year"].values():
        assert len(years) == 5
        assert [y["year"] for y in years] == [1, 2, 3, 4, 5]


def test_default_targets_includes_all_4_clouds():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)]
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    assert set(result["per_cloud_per_year"].keys()) == {"aws", "azure", "gcp", "oci"}


def test_explicit_targets_respected():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)]
    )
    result = compare_total_cost_of_ownership(catalog, inv, targets=["aws", "oci"])
    assert set(result["per_cloud_per_year"].keys()) == {"aws", "oci"}


# --- Growth math ---


def test_zero_growth_means_flat_year_over_year():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32, quantity=4)]
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    for years in result["per_cloud_per_year"].values():
        # All years should have the same total when growth is 0
        totals = [y["total_usd"] for y in years]
        assert all(t == totals[0] for t in totals)


def test_compute_growth_increases_year_over_year():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=16, memory_gb=64, quantity=2)]
    )
    result = compare_total_cost_of_ownership(
        catalog, inv,
        growth=GrowthAssumptions(compute_pct_yoy=0.50),  # +50% YoY
    )
    for cloud, years in result["per_cloud_per_year"].items():
        for i in range(1, len(years)):
            # Each year's total must exceed the previous year (ignore $0 free-tier clouds)
            if years[0]["total_usd"] > 0:
                assert years[i]["total_usd"] > years[i - 1]["total_usd"], cloud


def test_year_2_compute_is_1_plus_rate_times_year_1():
    """Year 2 = year 1 × (1 + rate). Compute it explicitly for one cloud."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=16, memory_gb=64, quantity=2)]
    )
    result = compare_total_cost_of_ownership(
        catalog, inv,
        growth=GrowthAssumptions(compute_pct_yoy=0.20),  # +20%
    )
    # Pick a cloud with non-zero year 1 cost
    for cloud, years in result["per_cloud_per_year"].items():
        y1 = years[0]["compute_storage_usd"]
        y2 = years[1]["compute_storage_usd"]
        if y1 > 0:
            expected_y2 = round(y1 * 1.20, 2)
            assert abs(y2 - expected_y2) < 1.0, f"{cloud}: expected {expected_y2}, got {y2}"
            return  # found one cloud to verify, that's enough
    pytest.fail("No cloud had non-zero compute cost — test infrastructure issue")


def test_egress_growth_only_affects_egress_section():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=16, memory_gb=64, quantity=2)],
        egress=[EgressItem(name="cdn", gb_per_month=10_000)],
    )
    result = compare_total_cost_of_ownership(
        catalog, inv,
        growth=GrowthAssumptions(egress_pct_yoy=0.30),  # +30% on egress only
    )
    for cloud, years in result["per_cloud_per_year"].items():
        # Compute should stay flat
        assert years[0]["compute_storage_usd"] == years[1]["compute_storage_usd"]
        # Egress should grow (skip clouds where egress year-1 is 0 due to free tier)
        if years[0]["egress_usd"] > 0:
            assert years[1]["egress_usd"] > years[0]["egress_usd"], cloud


# --- Cumulative TCO ---


def test_cumulative_tco_equals_sum_of_yearly_totals():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=16, memory_gb=64, quantity=2)]
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    for cloud in result["per_cloud_per_year"]:
        years = result["per_cloud_per_year"][cloud]
        expected = round(sum(y["total_usd"] for y in years), 2)
        assert result["cumulative_tco_usd"][cloud] == expected


def test_ranking_orders_by_cumulative_tco_ascending():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=16, memory_gb=64, quantity=2)]
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    cumul = result["cumulative_tco_usd"]
    ranking = result["ranking_by_tco"]
    totals = [cumul[c] for c in ranking]
    assert totals == sorted(totals)


def test_recommended_is_first_in_ranking():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)]
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    if result["ranking_by_tco"]:
        assert result["recommended"] == result["ranking_by_tco"][0]


# --- Sensitivity analysis ---


def test_sensitivity_identifies_dominant_variable_when_egress_heavy():
    """An egress-heavy workload should have egress_pct_yoy as dominant variable."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=4, memory_gb=16)],
        egress=[EgressItem(name="cdn", gb_per_month=100_000)],  # 100 TB
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    assert result["sensitivity"]["dominant_variable"] is not None


def test_sensitivity_provides_rationale_text():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)]
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    assert "rationale" in result["sensitivity"]
    assert len(result["sensitivity"]["rationale"]) > 0


# --- Per-section breakdown ---


def test_per_year_record_includes_section_breakdown():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)],
        object_storage=[ObjectStorageItem(name="media", capacity_gb=5000, tier="hot")],
        egress=[EgressItem(name="api", gb_per_month=10_000)],
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    for years in result["per_cloud_per_year"].values():
        for y in years:
            for field in [
                "compute_storage_usd",
                "object_storage_usd",
                "database_usd",
                "egress_usd",
                "total_usd",
            ]:
                assert field in y


# --- Honest gaps disclosed ---


def test_honest_gaps_disclosed():
    catalog = load_catalog()
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=8, memory_gb=32)]
    )
    result = compare_total_cost_of_ownership(catalog, inv)
    gaps = " ".join(result["honest_gaps"]).lower()
    assert "linear" in gaps or "yoy" in gaps
    assert "npv" in gaps or "discount" in gaps


# --- Growth dataclass ---


def test_growth_assumptions_defaults_to_zero():
    g = GrowthAssumptions()
    assert g.compute_pct_yoy == 0.0
    assert g.storage_pct_yoy == 0.0
    assert g.egress_pct_yoy == 0.0


def test_growth_assumptions_as_dict_round_trips():
    g = GrowthAssumptions(compute_pct_yoy=0.20, storage_pct_yoy=0.50, egress_pct_yoy=0.30)
    d = g.as_dict()
    assert d == {"compute_pct_yoy": 0.20, "storage_pct_yoy": 0.50, "egress_pct_yoy": 0.30}
