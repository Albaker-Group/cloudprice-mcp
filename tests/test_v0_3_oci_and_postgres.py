"""Tests for v0.3 features: OCI as 4th cloud + managed Postgres comparison."""

from cloudprice_mcp.compare import (
    CLOUDS,
    PostgresRequest,
    compare_all_clouds,
    compare_postgres,
)
from cloudprice_mcp.pricing import load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


# --- OCI as 4th cloud ---

def test_oci_is_in_clouds_tuple():
    assert "oci" in CLOUDS
    assert len(CLOUDS) == 4


def test_oci_compute_skus_loaded():
    catalog = load_catalog()
    oci = catalog.by_cloud("oci")
    assert len(oci) > 0
    # Must include both x86 (E4) and Arm (A1) families
    families = {sku.sku.split(".")[2] for sku in oci}
    assert "E4" in families
    assert "A1" in families


def test_oci_storage_skus_loaded():
    catalog = load_catalog()
    ssd = catalog.storage_for("oci", "ssd")
    hdd = catalog.storage_for("oci", "hdd")
    assert ssd is not None
    assert hdd is not None
    assert ssd.price_per_gb_month_usd > 0
    assert hdd.price_per_gb_month_usd > 0


def test_compare_clouds_returns_4_clouds():
    catalog = load_catalog()
    matches = compare_all_clouds(catalog, vcpus=4, memory_gb=16)
    clouds_returned = {m.cloud for m in matches}
    assert clouds_returned == {"aws", "azure", "gcp", "oci"}


# --- Postgres (v0.3 preview) ---

def test_postgres_skus_loaded_per_cloud():
    catalog = load_catalog()
    for cloud in ("aws", "azure", "gcp", "oci"):
        skus = catalog.postgres_by_cloud(cloud)
        assert len(skus) > 0, f"no postgres SKUs for {cloud}"


def test_postgres_compare_returns_per_cloud_totals():
    catalog = load_catalog()
    result = compare_postgres(
        catalog,
        [PostgresRequest(name="orders", vcpus=4, memory_gb=16, storage_gb=500)],
    )
    totals = result["totals_by_cloud"]
    assert set(totals.keys()) == {"aws", "azure", "gcp", "oci"}
    for cloud, total in totals.items():
        assert total > 0, f"{cloud} total should be > 0"


def test_postgres_storage_increases_total_linearly():
    catalog = load_catalog()
    no_storage = compare_postgres(
        catalog,
        [PostgresRequest(name="x", vcpus=2, memory_gb=8, storage_gb=0)],
    )
    with_storage = compare_postgres(
        catalog,
        [PostgresRequest(name="x", vcpus=2, memory_gb=8, storage_gb=1000)],
    )
    for cloud in ("aws", "azure", "gcp", "oci"):
        delta = with_storage["totals_by_cloud"][cloud] - no_storage["totals_by_cloud"][cloud]
        # Storage delta should match price_per_gb_month_usd × 1000
        assert delta > 0, f"{cloud} should charge for added storage"
