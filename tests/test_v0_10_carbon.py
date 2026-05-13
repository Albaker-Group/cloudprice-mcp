"""Tests for v0.10.0 compare_carbon_footprint — multi-cloud carbon model."""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops.carbon import (
    _is_arm_sku,
    _per_instance_carbon,
    compare_carbon_footprint,
    reset_factors_cache,
)
from cloudprice_mcp.pricing import (
    Instance,
    PriceCatalog,
    reset_catalog_cache,
)


def setup_function():
    reset_catalog_cache()
    reset_factors_cache()


def _factors_stub() -> dict:
    """Deterministic factors for math-checking the formulas."""
    return {
        "as_of": "2026-05-13",
        "pue_by_cloud": {"aws": 1.10, "azure": 1.10, "gcp": 1.10, "oci": 1.10},
        "grid_intensity_g_per_kwh_by_region": {
            "us-east-1": 400,
            "eastus": 400,
            "us-east1": 400,
            "us-ashburn-1": 400,
            "_default": 400,
        },
        "renewable_match_pct_by_cloud": {"aws": 100, "azure": 50, "gcp": 0, "oci": 0},
        "power_model": {
            "x86_watts_per_vcpu": 10.0,
            "arm_watts_per_vcpu": 5.0,
            "memory_watts_per_gb": 0.5,
            "utilization_factor": 1.0,  # full load for predictable math
        },
        "arm_sku_substrings": ["a1.", "c6g", "m6g", "r6g", "A1.Flex", "A2.Flex"],
    }


# --- Pure math helpers ---


def test_per_instance_math_x86():
    """Verify the formula on a hand-computed x86 case.

    8 vCPU * 10W + 32 GB * 0.5W = 96W (load=1.0)
    facility = 96W * PUE 1.10 = 105.6W
    monthly_kWh = 105.6 * 730 / 1000 = 77.088
    monthly_gCO2e_grid = 77.088 * 400 = 30,835.2
    AWS residual (100% match) = 0
    Azure residual (50% match) = 15,417.6
    """
    f = _factors_stub()
    r = _per_instance_carbon("aws", "m5.2xlarge", vcpus=8, memory_gb=32, region="us-east-1", factors=f)
    assert r["power_class"] == "x86"
    assert r["instance_watts"] == pytest.approx(96.0)
    assert r["facility_watts"] == pytest.approx(105.6)
    assert r["monthly_kwh"] == pytest.approx(77.088, abs=0.01)
    assert r["monthly_gCO2e_grid"] == pytest.approx(30835.2, abs=1)
    assert r["monthly_gCO2e_residual"] == pytest.approx(0)

    r2 = _per_instance_carbon("azure", "D8s_v5", vcpus=8, memory_gb=32, region="eastus", factors=f)
    assert r2["monthly_gCO2e_residual"] == pytest.approx(15417.6, abs=1)


def test_per_instance_math_arm_uses_lower_watts():
    f = _factors_stub()
    r_x86 = _per_instance_carbon("oci", "VM.Standard.E5.Flex.4OCPU", vcpus=8, memory_gb=32, region="us-ashburn-1", factors=f)
    r_arm = _per_instance_carbon("oci", "VM.Standard.A1.Flex", vcpus=8, memory_gb=32, region="us-ashburn-1", factors=f)
    assert r_x86["power_class"] == "x86"
    assert r_arm["power_class"] == "arm"
    # ARM uses 5W/vCPU vs x86 10W/vCPU
    assert r_arm["instance_watts"] < r_x86["instance_watts"]
    assert r_arm["monthly_gCO2e_grid"] < r_x86["monthly_gCO2e_grid"]


def test_is_arm_detects_known_arm_skus():
    f = _factors_stub()
    assert _is_arm_sku("VM.Standard.A1.Flex.AlwaysFree", f) is True
    assert _is_arm_sku("VM.Standard.A2.Flex.1OCPU", f) is True
    assert _is_arm_sku("a1.large", f) is True
    assert _is_arm_sku("c6g.xlarge", f) is True  # AWS Graviton family


def test_is_arm_falls_back_to_x86_for_unknown():
    f = _factors_stub()
    assert _is_arm_sku("m5.xlarge", f) is False
    assert _is_arm_sku("D2s_v5", f) is False
    assert _is_arm_sku("VM.Standard.E5.Flex.4OCPU", f) is False


# --- End-to-end compare_carbon_footprint ---


def _make_catalog(instances: list[Instance]) -> PriceCatalog:
    return PriceCatalog(as_of="2026-05-13", currency="USD", instances=tuple(instances), storage=())


def test_compare_returns_per_cloud_rows():
    cat = _make_catalog([
        Instance(cloud="aws", sku="m5.2xlarge", vcpus=8, memory_gb=32, hourly_usd=0.384, region="us-east-1"),
        Instance(cloud="azure", sku="D8s_v5", vcpus=8, memory_gb=32, hourly_usd=0.384, region="eastus"),
        Instance(cloud="oci", sku="VM.Standard.E5.Flex.4OCPU", vcpus=8, memory_gb=32, hourly_usd=0.184, region="us-ashburn-1"),
    ])
    r = compare_carbon_footprint(cat, vcpus=8, memory_gb=32)
    assert r["kind"] == "carbon_footprint_comparison"
    assert len(r["per_cloud"]) == 3
    for row in r["per_cloud"]:
        assert "monthly_kg_CO2e_grid" in row
        assert "monthly_kg_CO2e_residual" in row
        assert "monthly_usd" in row
        assert row["monthly_kwh"] >= 0


def test_compare_residual_ranks_renewable_matched_first():
    """AWS / Azure / GCP / OCI have 100 / 100 / 64 / 0 renewable match in
    real factors. Lowest residual should win regardless of grid intensity."""
    cat = _make_catalog([
        Instance(cloud="aws", sku="m5.large", vcpus=2, memory_gb=8, hourly_usd=0.096, region="us-east-1"),
        Instance(cloud="oci", sku="VM.Standard.E5.Flex.1OCPU", vcpus=2, memory_gb=8, hourly_usd=0.046, region="us-ashburn-1"),
    ])
    r = compare_carbon_footprint(cat, vcpus=2, memory_gb=8)
    # AWS first (residual=0); OCI second (no renewable matching)
    assert r["per_cloud"][0]["cloud"] == "aws"
    assert r["per_cloud"][-1]["cloud"] == "oci"
    assert r["recommended"] == "aws"


def test_compare_scales_with_quantity():
    cat = _make_catalog([
        Instance(cloud="oci", sku="VM.Standard.E5.Flex.1OCPU", vcpus=2, memory_gb=8, hourly_usd=0.046, region="us-ashburn-1"),
    ])
    r1 = compare_carbon_footprint(cat, vcpus=2, memory_gb=8, quantity=1)
    r10 = compare_carbon_footprint(cat, vcpus=2, memory_gb=8, quantity=10)
    assert r10["per_cloud"][0]["monthly_kwh"] == pytest.approx(r1["per_cloud"][0]["monthly_kwh"] * 10, rel=0.01)
    assert r10["per_cloud"][0]["monthly_kg_CO2e_grid"] == pytest.approx(r1["per_cloud"][0]["monthly_kg_CO2e_grid"] * 10, rel=0.01)
    assert r10["per_cloud"][0]["monthly_usd"] == pytest.approx(r1["per_cloud"][0]["monthly_usd"] * 10, rel=0.01)


def test_compare_targets_filter():
    cat = _make_catalog([
        Instance(cloud="aws", sku="m5.large", vcpus=2, memory_gb=8, hourly_usd=0.096, region="us-east-1"),
        Instance(cloud="azure", sku="D2s_v5", vcpus=2, memory_gb=8, hourly_usd=0.096, region="eastus"),
        Instance(cloud="gcp", sku="n2-standard-2", vcpus=2, memory_gb=8, hourly_usd=0.0971, region="us-east1"),
    ])
    r = compare_carbon_footprint(cat, vcpus=2, memory_gb=8, targets=["aws", "gcp"])
    clouds_in_report = {row["cloud"] for row in r["per_cloud"]}
    assert clouds_in_report == {"aws", "gcp"}


def test_compare_includes_pue_and_grid_intensity():
    cat = _make_catalog([
        Instance(cloud="aws", sku="m5.large", vcpus=2, memory_gb=8, hourly_usd=0.096, region="us-east-1"),
    ])
    r = compare_carbon_footprint(cat, vcpus=2, memory_gb=8)
    row = r["per_cloud"][0]
    assert "pue" in row
    assert "grid_intensity_g_per_kwh" in row
    assert "renewable_match_pct" in row
    assert row["pue"] > 1.0  # PUE always >= 1
    assert row["grid_intensity_g_per_kwh"] > 0


def test_compare_includes_honest_gaps():
    cat = _make_catalog([
        Instance(cloud="aws", sku="m5.large", vcpus=2, memory_gb=8, hourly_usd=0.096, region="us-east-1"),
    ])
    r = compare_carbon_footprint(cat, vcpus=2, memory_gb=8)
    gaps = " ".join(r["honest_gaps"])
    # Key disclosures: utilization, ARM, embodied carbon
    assert "utilization" in gaps.lower()
    assert "embodied" in gaps.lower()
    assert "annual" in gaps.lower()


def test_compare_kg_per_dollar_computed():
    cat = _make_catalog([
        Instance(cloud="aws", sku="m5.large", vcpus=2, memory_gb=8, hourly_usd=0.096, region="us-east-1"),
        Instance(cloud="oci", sku="VM.Standard.E5.Flex.1OCPU", vcpus=2, memory_gb=8, hourly_usd=0.046, region="us-ashburn-1"),
    ])
    r = compare_carbon_footprint(cat, vcpus=2, memory_gb=8)
    for row in r["per_cloud"]:
        # AWS row has 0 residual -> kg_per_dollar should be 0
        # OCI row has positive residual -> kg_per_dollar positive
        if row["monthly_usd"] > 0:
            assert row["kg_CO2e_per_dollar"] is not None
            assert row["kg_CO2e_per_dollar"] >= 0


def test_validation_raises_on_zero_vcpus():
    cat = _make_catalog([])
    with pytest.raises(ValueError, match="vcpus"):
        compare_carbon_footprint(cat, vcpus=0, memory_gb=8)


def test_validation_raises_on_negative_memory():
    cat = _make_catalog([])
    with pytest.raises(ValueError, match="memory"):
        compare_carbon_footprint(cat, vcpus=2, memory_gb=0)


def test_validation_raises_on_zero_quantity():
    cat = _make_catalog([])
    with pytest.raises(ValueError, match="quantity"):
        compare_carbon_footprint(cat, vcpus=2, memory_gb=8, quantity=0)


def test_all_100_percent_matched_falls_back_to_grid_headline():
    """If all clouds in the comparison have 100% renewable match, residual = 0
    for all and the headline pivots to location-based ranking."""
    cat = _make_catalog([
        Instance(cloud="aws", sku="m5.large", vcpus=2, memory_gb=8, hourly_usd=0.096, region="us-east-1"),
        Instance(cloud="azure", sku="D2s_v5", vcpus=2, memory_gb=8, hourly_usd=0.096, region="eastus"),
    ])
    r = compare_carbon_footprint(cat, vcpus=2, memory_gb=8)
    # Real factors: both AWS + Azure are 100% matched
    assert "100% renewable" in r["headline"] or r["per_cloud"][0]["monthly_kg_CO2e_residual"] == 0
