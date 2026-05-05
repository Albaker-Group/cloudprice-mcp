"""Tests for v0.3 object storage compare across AWS / Azure / GCP / OCI."""

from cloudprice_mcp.compare import (
    CLOUDS,
    ObjectStorageRequest,
    compare_object_storage,
)
from cloudprice_mcp.pricing import load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


def test_object_storage_skus_loaded_per_cloud():
    catalog = load_catalog()
    for cloud in CLOUDS:
        skus = catalog.object_storage_by_cloud(cloud)
        assert len(skus) > 0, f"no object storage SKUs for {cloud}"
        # Each cloud must have hot, cool, archive
        tiers = {sku.tier for sku in skus}
        assert "hot" in tiers
        assert "cool" in tiers
        assert "archive" in tiers


def test_oci_has_always_free_object_storage_sku():
    catalog = load_catalog()
    oci_sks = catalog.object_storage_by_cloud("oci")
    free_skus = [s for s in oci_sks if s.capacity_gb_limit is not None]
    assert len(free_skus) >= 1
    # The Always-Free OCI SKU must have $0 price and ~20 GB limit
    free = free_skus[0]
    assert free.price_per_gb_month_usd == 0.0
    assert free.capacity_gb_limit == 20


def test_compare_object_storage_returns_4_clouds():
    catalog = load_catalog()
    result = compare_object_storage(
        catalog,
        [ObjectStorageRequest(name="app-data", capacity_gb=1000, tier="hot")],
    )
    totals = result["totals_by_cloud"]
    assert set(totals.keys()) == {"aws", "azure", "gcp", "oci"}
    for cloud, total in totals.items():
        # 1000 GB hot tier should be > $0 on every paid cloud
        assert total > 0


def test_oci_always_free_kicks_in_when_under_20gb():
    catalog = load_catalog()
    result = compare_object_storage(
        catalog,
        [ObjectStorageRequest(name="small", capacity_gb=15, tier="hot")],
    )
    # OCI should be $0 at this capacity (within Always Free limit of 20 GB)
    assert result["totals_by_cloud"]["oci"] == 0.0
    # Other clouds should NOT be $0 — they don't have a free tier in our dataset
    assert result["totals_by_cloud"]["aws"] > 0
    assert result["totals_by_cloud"]["azure"] > 0
    assert result["totals_by_cloud"]["gcp"] > 0


def test_oci_always_free_does_not_apply_when_over_20gb():
    catalog = load_catalog()
    result = compare_object_storage(
        catalog,
        [ObjectStorageRequest(name="too-big", capacity_gb=100, tier="hot")],
    )
    # 100 GB > 20 GB Always Free limit → falls back to paid Standard rate
    assert result["totals_by_cloud"]["oci"] > 0


def test_archive_tier_cheaper_than_hot_tier():
    catalog = load_catalog()
    hot = compare_object_storage(
        catalog, [ObjectStorageRequest(name="x", capacity_gb=1000, tier="hot")]
    )
    archive = compare_object_storage(
        catalog, [ObjectStorageRequest(name="x", capacity_gb=1000, tier="archive")]
    )
    for cloud in ("aws", "azure", "gcp", "oci"):
        # OCI archive may be skipped if free tier handled differently — guard for it
        if archive["totals_by_cloud"][cloud] > 0 and hot["totals_by_cloud"][cloud] > 0:
            assert archive["totals_by_cloud"][cloud] < hot["totals_by_cloud"][cloud], (
                f"{cloud}: archive should be cheaper than hot"
            )


def test_quantity_multiplies_total():
    catalog = load_catalog()
    one = compare_object_storage(
        catalog, [ObjectStorageRequest(name="x", capacity_gb=500, tier="hot", quantity=1)]
    )
    five = compare_object_storage(
        catalog, [ObjectStorageRequest(name="x", capacity_gb=500, tier="hot", quantity=5)]
    )
    for cloud in ("aws", "azure", "gcp", "oci"):
        if one["totals_by_cloud"][cloud] > 0:
            assert (
                round(five["totals_by_cloud"][cloud], 2)
                == round(one["totals_by_cloud"][cloud] * 5, 2)
            )
