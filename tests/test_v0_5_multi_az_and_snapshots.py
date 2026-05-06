"""Tests for v0.5 multi-AZ multiplier + snapshot incremental factor."""
from __future__ import annotations

from cloudprice_mcp.compare import (
    MULTI_AZ_COMPUTE_MULTIPLIER,
    ComputeRequest,
    StorageRequest,
    bulk_compare_storage,
    compare_workload,
)
from cloudprice_mcp.pricing import load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


# --- Multi-AZ multiplier ---

def test_multi_az_doubles_compute_totals():
    catalog = load_catalog()
    compute = [ComputeRequest(name="api", vcpus=4, memory_gb=16, quantity=2)]
    base = compare_workload(catalog, compute, [], multi_az=False)
    multi = compare_workload(catalog, compute, [], multi_az=True)

    for cloud in ("aws", "azure", "gcp", "oci"):
        base_total = base["compute"]["totals_by_cloud"][cloud]
        multi_total = multi["compute"]["totals_by_cloud"][cloud]
        expected_multiplier = MULTI_AZ_COMPUTE_MULTIPLIER[cloud]
        assert abs(multi_total - base_total * expected_multiplier) < 0.05


def test_multi_az_does_not_double_storage():
    catalog = load_catalog()
    storage = [StorageRequest(name="data", capacity_gb=1000, disk_type="ssd")]
    base = compare_workload(catalog, [], storage, multi_az=False)
    multi = compare_workload(catalog, [], storage, multi_az=True)
    for cloud in ("aws", "azure", "gcp", "oci"):
        # Storage stays at 1x even with multi_az=True
        assert (
            base["storage"]["totals_by_cloud"][cloud]
            == multi["storage"]["totals_by_cloud"][cloud]
        )


def test_multi_az_flag_appears_in_output():
    catalog = load_catalog()
    compute = [ComputeRequest(name="x", vcpus=2, memory_gb=4)]
    out = compare_workload(catalog, compute, [], multi_az=True)
    assert out.get("multi_az_applied") is True
    assert out["compute"].get("multi_az_applied") is True
    assert "multi_az_multipliers" in out["compute"]


def test_multi_az_default_is_off():
    catalog = load_catalog()
    compute = [ComputeRequest(name="x", vcpus=2, memory_gb=4)]
    out = compare_workload(catalog, compute, [])
    assert out.get("multi_az_applied") is None
    assert out["compute"].get("multi_az_applied") is None


def test_multi_az_combines_correctly_with_commitment():
    catalog = load_catalog()
    compute = [ComputeRequest(name="api", vcpus=8, memory_gb=32, quantity=4)]
    out = compare_workload(catalog, compute, [], multi_az=True, commitment="3yr_partial_upfront")
    # Multi-AZ doubles compute, then 3yr commitment cuts compute by 50%
    # Net: compute should be roughly equal to base on-demand (2 × 0.5 = 1.0)
    base = compare_workload(catalog, compute, [])
    for cloud in ("aws", "azure", "gcp", "oci"):
        committed_total = out["commitment"]["totals_by_cloud"][cloud]
        base_total = base["combined"]["totals_by_cloud"][cloud]
        # Within 5% — same ballpark
        assert abs(committed_total - base_total) / base_total < 0.10


# --- Snapshot incremental factor ---

def test_snapshot_incremental_factor_default_is_upper_bound():
    catalog = load_catalog()
    volumes = [StorageRequest(name="x", capacity_gb=1000, snapshot_count=7)]
    result = bulk_compare_storage(catalog, volumes)
    # Default factor 1.0 = full upper-bound (snapshot_count × full capacity)
    aws_ssd = catalog.storage_for("aws", "ssd")
    expected_aws = round(
        aws_ssd.snapshot_per_gb_month_usd * 1000 * 7 + aws_ssd.price_per_gb_month_usd * 1000,
        2,
    )
    # Total includes capacity cost + 7 snapshots at full capacity
    assert abs(result["totals_by_cloud"]["aws"] - expected_aws) < 0.05


def test_snapshot_incremental_factor_30pct_is_realistic_estimate():
    catalog = load_catalog()
    volumes = [
        StorageRequest(
            name="x",
            capacity_gb=1000,
            snapshot_count=7,
            snapshot_incremental_factor=0.3,  # typical real-world incremental
        )
    ]
    result = bulk_compare_storage(catalog, volumes)
    aws_ssd = catalog.storage_for("aws", "ssd")
    # 0.3 factor means snapshots cost 30% of upper-bound
    expected_snapshot = aws_ssd.snapshot_per_gb_month_usd * 1000 * 7 * 0.3
    expected_capacity = aws_ssd.price_per_gb_month_usd * 1000
    expected_aws = round(expected_snapshot + expected_capacity, 2)
    assert abs(result["totals_by_cloud"]["aws"] - expected_aws) < 0.05


def test_snapshot_incremental_factor_zero_means_no_snapshot_charge():
    catalog = load_catalog()
    volumes = [
        StorageRequest(
            name="x",
            capacity_gb=1000,
            snapshot_count=7,
            snapshot_incremental_factor=0.0,
        )
    ]
    result = bulk_compare_storage(catalog, volumes)
    aws_ssd = catalog.storage_for("aws", "ssd")
    # With factor 0, snapshot cost is 0 — only capacity cost remains
    expected_aws = round(aws_ssd.price_per_gb_month_usd * 1000, 2)
    assert abs(result["totals_by_cloud"]["aws"] - expected_aws) < 0.05
