"""Tests for v0.5 compare_egress feature."""
from __future__ import annotations

from cloudprice_mcp.compare import (
    CLOUDS,
    EgressRequest,
    compare_egress,
)
from cloudprice_mcp.pricing import EgressPricing, EgressTier, load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


# --- Pricing data loaded ---

def test_egress_data_loaded_for_all_4_clouds():
    catalog = load_catalog()
    for cloud in CLOUDS:
        sku = catalog.egress_for(cloud)
        assert sku is not None, f"no egress data for {cloud}"
        assert sku.cloud == cloud
        assert len(sku.tiers) > 0


def test_aws_and_azure_have_100gb_free_tier():
    catalog = load_catalog()
    for cloud in ("aws", "azure"):
        sku = catalog.egress_for(cloud)
        assert sku is not None
        assert sku.free_tier_gb == 100


def test_oci_has_10tb_free_tier_killer_feature():
    catalog = load_catalog()
    sku = catalog.egress_for("oci")
    assert sku is not None
    assert sku.free_tier_gb == 10000  # 10 TB


def test_gcp_has_no_flat_free_tier():
    catalog = load_catalog()
    sku = catalog.egress_for("gcp")
    assert sku is not None
    assert sku.free_tier_gb == 0


# --- Cost calculation ---

def test_cost_zero_when_within_free_tier():
    sku = EgressPricing(
        cloud="aws",
        service="test",
        region="us-east-1",
        free_tier_gb=100,
        tiers=(EgressTier(up_to_gb=10000, price_per_gb_usd=0.09),),
    )
    assert sku.cost_for_gb(50) < 0.01
    assert sku.cost_for_gb(100) < 0.01


def test_cost_only_billed_above_free_tier():
    sku = EgressPricing(
        cloud="aws",
        service="test",
        region="us-east-1",
        free_tier_gb=100,
        tiers=(EgressTier(up_to_gb=10000, price_per_gb_usd=0.09),),
    )
    # 200 GB = 100 free + 100 billed × $0.09 = $9
    assert abs(sku.cost_for_gb(200) - 9.0) < 0.01


def test_tiered_pricing_steps_down_at_thresholds():
    sku = EgressPricing(
        cloud="test",
        service="test",
        region="r",
        free_tier_gb=100,
        tiers=(
            EgressTier(up_to_gb=10100, price_per_gb_usd=0.10),  # 10 TB at $0.10
            EgressTier(up_to_gb=50100, price_per_gb_usd=0.05),  # next 40 TB at $0.05
            EgressTier(up_to_gb=None, price_per_gb_usd=0.02),
        ),
    )
    # 100 GB free + 10 TB at $0.10 + 1 TB at $0.05 = $1000 + $50 = $1050
    assert abs(sku.cost_for_gb(11100) - 1050.0) < 0.01


def test_unbounded_final_tier_handles_huge_volume():
    sku = EgressPricing(
        cloud="test",
        service="test",
        region="r",
        free_tier_gb=0,
        tiers=(EgressTier(up_to_gb=None, price_per_gb_usd=0.01),),
    )
    assert abs(sku.cost_for_gb(1_000_000) - 10000.0) < 0.01


# --- Realistic comparisons ---

def test_compare_egress_at_1tb_aws_azure_close_oci_free():
    catalog = load_catalog()
    result = compare_egress(catalog, [EgressRequest(name="x", gb_per_month=1024)])
    totals = result["totals_by_cloud"]
    # OCI: 1 TB << 10 TB free → $0
    assert totals["oci"] < 0.01
    # AWS/Azure: ~924 GB billed × ~$0.09 ≈ $80
    assert 75 < totals["aws"] < 90
    assert 75 < totals["azure"] < 90
    # GCP: full 1024 × $0.12 = $122.88 (no free tier in our model)
    assert 120 < totals["gcp"] < 125


def test_compare_egress_at_50tb_oci_dominates():
    catalog = load_catalog()
    result = compare_egress(catalog, [EgressRequest(name="big", gb_per_month=50000)])
    totals = result["totals_by_cloud"]
    # At 50 TB:
    # - OCI: 10 TB free + 40 TB × $0.0085 = $340
    # - AWS/Azure/GCP: thousands of dollars
    assert totals["oci"] < 500
    assert totals["aws"] > 3000
    assert totals["azure"] > 3000
    assert totals["gcp"] > 3000
    # OCI should be cheapest by a huge margin
    assert result["cheapest_cloud"] == "oci"
    # Savings should be massive (>80%)
    assert result["savings_vs_priciest_pct"] > 80


# --- Inter-region transfer ---

def test_inter_region_transfer_priced_per_cloud():
    catalog = load_catalog()
    for cloud in ("aws", "azure", "gcp", "oci"):
        sku = catalog.egress_for(cloud)
        assert sku is not None
        assert sku.inter_region_per_gb_usd > 0


def test_inter_region_no_free_tier():
    catalog = load_catalog()
    sku = catalog.egress_for("aws")
    assert sku is not None
    # 100 GB inter-region should cost something — no free tier on cross-region
    assert sku.inter_region_cost_for_gb(100) > 0


def test_inter_region_oci_cheaper_than_hyperscalers():
    catalog = load_catalog()
    aws = catalog.egress_for("aws")
    oci = catalog.egress_for("oci")
    assert aws is not None and oci is not None
    # OCI inter-region $0.0085/GB; AWS $0.02/GB; OCI ~58% cheaper
    aws_cost = aws.inter_region_cost_for_gb(1000)
    oci_cost = oci.inter_region_cost_for_gb(1000)
    assert oci_cost < aws_cost
    assert oci_cost < aws_cost * 0.5
