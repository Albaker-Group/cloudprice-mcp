"""Tests for v0.8.1 compare_spot — multi-cloud spot pricing comparison."""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops.spot import compare_spot
from cloudprice_mcp.pricing import (
    HOURS_PER_MONTH,
    Instance,
    PriceCatalog,
    reset_catalog_cache,
)


def setup_function():
    reset_catalog_cache()


def _make_catalog(instances: list[Instance]) -> PriceCatalog:
    return PriceCatalog(
        as_of="2026-05-13",
        currency="USD",
        instances=tuple(instances),
        storage=(),
    )


def test_compare_spot_returns_per_cloud_rows():
    catalog = _make_catalog([
        Instance(cloud="aws", sku="m5.xlarge", vcpus=4, memory_gb=16,
                 hourly_usd=0.192, region="us-east-1", spot_hourly_usd=0.06),
        Instance(cloud="azure", sku="D4s_v5", vcpus=4, memory_gb=16,
                 hourly_usd=0.192, region="eastus", spot_hourly_usd=0.04),
        Instance(cloud="oci", sku="VM.Standard.E5.Flex.2OCPU", vcpus=4, memory_gb=16,
                 hourly_usd=0.184, region="us-ashburn-1", spot_hourly_usd=0.092),
    ])
    result = compare_spot(catalog, vcpus=4, memory_gb=16)
    assert result["kind"] == "spot_comparison"
    assert len(result["per_cloud"]) == 3
    # Cheapest spot first
    assert result["per_cloud"][0]["cloud"] == "azure"
    assert result["per_cloud"][0]["spot_hourly_usd"] == pytest.approx(0.04)
    assert result["recommended"] == "azure"


def test_compare_spot_computes_savings_correctly():
    catalog = _make_catalog([
        Instance(cloud="aws", sku="m5.xlarge", vcpus=4, memory_gb=16,
                 hourly_usd=0.192, region="us-east-1", spot_hourly_usd=0.048),
    ])
    result = compare_spot(catalog, vcpus=4, memory_gb=16)
    row = result["per_cloud"][0]
    assert row["savings_pct"] == pytest.approx(75.0)
    assert row["ondemand_monthly_usd"] == pytest.approx(0.192 * HOURS_PER_MONTH, abs=0.01)
    expected_monthly_savings = round((0.192 - 0.048) * HOURS_PER_MONTH, 2)
    assert row["monthly_savings_usd"] == pytest.approx(expected_monthly_savings, abs=0.01)


def test_compare_spot_handles_missing_spot_data():
    """A cloud without spot_hourly_usd should still appear with on-demand info
    but be ranked after clouds that do have spot."""
    catalog = _make_catalog([
        Instance(cloud="aws", sku="m5.xlarge", vcpus=4, memory_gb=16,
                 hourly_usd=0.192, region="us-east-1", spot_hourly_usd=0.06),
        Instance(cloud="gcp", sku="n2-standard-4", vcpus=4, memory_gb=16,
                 hourly_usd=0.20, region="us-east1", spot_hourly_usd=None),
    ])
    result = compare_spot(catalog, vcpus=4, memory_gb=16)
    assert "gcp" in result["clouds_without_spot_data"]
    # AWS has spot, should rank ahead of GCP
    assert result["per_cloud"][0]["cloud"] == "aws"
    assert result["per_cloud"][1]["cloud"] == "gcp"
    assert result["per_cloud"][1]["spot_hourly_usd"] is None
    assert "spot_unavailable_reason" in result["per_cloud"][1]


def test_compare_spot_includes_eviction_class_per_cloud():
    catalog = _make_catalog([
        Instance(cloud="aws", sku="m5.xlarge", vcpus=4, memory_gb=16,
                 hourly_usd=0.192, region="us-east-1", spot_hourly_usd=0.06),
        Instance(cloud="oci", sku="VM.Standard.E5.Flex.2OCPU", vcpus=4, memory_gb=16,
                 hourly_usd=0.184, region="us-ashburn-1", spot_hourly_usd=0.092),
    ])
    result = compare_spot(catalog, vcpus=4, memory_gb=16)
    for row in result["per_cloud"]:
        assert "eviction" in row
        assert "class" in row["eviction"]
        assert "max_lifetime" in row["eviction"]
        # Sanity: OCI's 24h cap is surfaced
        if row["cloud"] == "oci":
            assert row["eviction"]["max_lifetime"] == "24 hours"


def test_compare_spot_targets_filter():
    catalog = _make_catalog([
        Instance(cloud="aws", sku="m5.xlarge", vcpus=4, memory_gb=16,
                 hourly_usd=0.192, region="us-east-1", spot_hourly_usd=0.06),
        Instance(cloud="azure", sku="D4s_v5", vcpus=4, memory_gb=16,
                 hourly_usd=0.192, region="eastus", spot_hourly_usd=0.04),
        Instance(cloud="oci", sku="VM.Standard.E5.Flex.2OCPU", vcpus=4, memory_gb=16,
                 hourly_usd=0.184, region="us-ashburn-1", spot_hourly_usd=0.092),
    ])
    result = compare_spot(catalog, vcpus=4, memory_gb=16, targets=["aws", "oci"])
    clouds = [r["cloud"] for r in result["per_cloud"]]
    assert set(clouds) == {"aws", "oci"}


def test_compare_spot_headline_mentions_recommended_and_savings():
    catalog = _make_catalog([
        Instance(cloud="aws", sku="m5.xlarge", vcpus=4, memory_gb=16,
                 hourly_usd=0.192, region="us-east-1", spot_hourly_usd=0.048),
    ])
    result = compare_spot(catalog, vcpus=4, memory_gb=16)
    headline = result["headline"]
    assert "AWS" in headline
    assert "m5.xlarge" in headline
    assert "75" in headline  # savings_pct


def test_compare_spot_honest_gaps_includes_eviction_warning():
    catalog = _make_catalog([
        Instance(cloud="oci", sku="VM.Standard.E5.Flex.1OCPU", vcpus=2, memory_gb=8,
                 hourly_usd=0.046, region="us-ashburn-1", spot_hourly_usd=0.023),
    ])
    result = compare_spot(catalog, vcpus=2, memory_gb=8)
    gaps_text = " ".join(result["honest_gaps"])
    assert "eviction" in gaps_text.lower()
    assert "24-hour" in gaps_text


def test_compare_spot_all_clouds_missing_spot_yields_warning_headline():
    catalog = _make_catalog([
        Instance(cloud="aws", sku="m5.xlarge", vcpus=4, memory_gb=16,
                 hourly_usd=0.192, region="us-east-1", spot_hourly_usd=None),
        Instance(cloud="gcp", sku="n2-standard-4", vcpus=4, memory_gb=16,
                 hourly_usd=0.20, region="us-east1", spot_hourly_usd=None),
    ])
    result = compare_spot(catalog, vcpus=4, memory_gb=16)
    assert "No spot pricing available" in result["headline"]
    assert set(result["clouds_without_spot_data"]) == {"aws", "gcp"}
