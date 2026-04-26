from cloudprice_mcp.compare import (
    COMMITMENT_DISCOUNT,
    ComputeRequest,
    StorageRequest,
    best_match,
    bulk_compare_compute,
    bulk_compare_storage,
    compare_all_clouds,
    compare_workload,
)
from cloudprice_mcp.pricing import HOURS_PER_MONTH, load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


# --- single match ---

def test_best_match_meets_or_exceeds_spec():
    catalog = load_catalog()
    match = best_match(catalog, "aws", vcpus=4, memory_gb=16)
    assert match is not None
    assert match.instance.vcpus >= 4
    assert match.instance.memory_gb >= 16


def test_compare_returns_one_per_cloud_sorted_by_price():
    catalog = load_catalog()
    matches = compare_all_clouds(catalog, vcpus=4, memory_gb=16)
    assert len(matches) == 3
    clouds = {m.cloud for m in matches}
    assert clouds == {"aws", "azure", "gcp"}
    prices = [m.instance.monthly_usd for m in matches]
    assert prices == sorted(prices)


def test_cheapest_picked_when_two_equally_match():
    catalog = load_catalog()
    matches = compare_all_clouds(catalog, vcpus=2, memory_gb=4)
    aws_match = next(m for m in matches if m.cloud == "aws")
    aws_candidates = [
        i for i in catalog.by_cloud("aws")
        if i.vcpus >= 2 and i.memory_gb >= 4
    ]
    cheapest_eligible = min(aws_candidates, key=lambda i: i.hourly_usd)
    assert aws_match.instance.sku == cheapest_eligible.sku


# --- bulk compute ---

def test_bulk_compute_aggregates_correctly():
    catalog = load_catalog()
    workloads = [
        ComputeRequest(name="web", vcpus=4, memory_gb=16, quantity=3),
        ComputeRequest(name="db", vcpus=8, memory_gb=32, quantity=1),
    ]
    result = bulk_compare_compute(catalog, workloads)
    assert "rows" in result
    assert len(result["rows"]) == 2
    assert "totals_by_cloud" in result
    for cloud in ("aws", "azure", "gcp"):
        # totals must equal the sum of row_monthly_total per cloud
        per_cloud_sum = sum(
            row["per_cloud"][cloud]["row_monthly_total"]
            for row in result["rows"]
            if cloud in row["per_cloud"]
        )
        assert round(per_cloud_sum, 2) == round(result["totals_by_cloud"][cloud], 2)


def test_bulk_compute_quantity_multiplies_total():
    catalog = load_catalog()
    one = bulk_compare_compute(catalog, [ComputeRequest(name="x", vcpus=4, memory_gb=16, quantity=1)])
    five = bulk_compare_compute(catalog, [ComputeRequest(name="x", vcpus=4, memory_gb=16, quantity=5)])
    for cloud in ("aws", "azure", "gcp"):
        assert round(five["totals_by_cloud"][cloud], 2) == round(one["totals_by_cloud"][cloud] * 5, 2)


def test_bulk_compute_part_time_hours_scale_cost():
    catalog = load_catalog()
    full = bulk_compare_compute(
        catalog, [ComputeRequest(name="dev", vcpus=2, memory_gb=4, hours_per_month=730)]
    )
    half = bulk_compare_compute(
        catalog, [ComputeRequest(name="dev", vcpus=2, memory_gb=4, hours_per_month=365)]
    )
    for cloud in ("aws", "azure", "gcp"):
        # Allow up to $0.02 rounding drift since each cloud's row is independently rounded.
        assert abs(half["totals_by_cloud"][cloud] - full["totals_by_cloud"][cloud] / 2) < 0.02


def test_bulk_compute_picks_cheapest_cloud():
    catalog = load_catalog()
    result = bulk_compare_compute(
        catalog, [ComputeRequest(name="x", vcpus=4, memory_gb=16)]
    )
    totals = result["totals_by_cloud"]
    assert result["cheapest_cloud"] == min(totals, key=lambda c: totals[c])


def test_bulk_compute_includes_os_disk_when_specified():
    catalog = load_catalog()
    no_disk = bulk_compare_compute(
        catalog, [ComputeRequest(name="x", vcpus=4, memory_gb=16)]
    )
    with_disk = bulk_compare_compute(
        catalog,
        [ComputeRequest(name="x", vcpus=4, memory_gb=16, os_disk_gb=100, os_disk_type="ssd")],
    )
    for cloud in ("aws", "azure", "gcp"):
        ssd = catalog.storage_for(cloud, "ssd")
        assert ssd is not None
        expected_disk = round(ssd.price_per_gb_month_usd * 100, 2)
        delta = round(with_disk["totals_by_cloud"][cloud] - no_disk["totals_by_cloud"][cloud], 2)
        assert delta == expected_disk


# --- bulk storage ---

def test_bulk_storage_uses_per_gb_pricing():
    catalog = load_catalog()
    volumes = [StorageRequest(name="vol1", capacity_gb=500, disk_type="ssd", quantity=2)]
    result = bulk_compare_storage(catalog, volumes)
    for cloud in ("aws", "azure", "gcp"):
        ssd = catalog.storage_for(cloud, "ssd")
        assert ssd is not None
        expected = round(ssd.price_per_gb_month_usd * 500 * 2, 2)
        assert round(result["totals_by_cloud"][cloud], 2) == expected


def test_bulk_storage_ssd_is_more_expensive_than_hdd():
    catalog = load_catalog()
    ssd_result = bulk_compare_storage(
        catalog, [StorageRequest(name="x", capacity_gb=1000, disk_type="ssd")]
    )
    hdd_result = bulk_compare_storage(
        catalog, [StorageRequest(name="x", capacity_gb=1000, disk_type="hdd")]
    )
    for cloud in ("aws", "azure", "gcp"):
        assert ssd_result["totals_by_cloud"][cloud] >= hdd_result["totals_by_cloud"][cloud]


def test_bulk_storage_charges_snapshots_v021():
    catalog = load_catalog()
    no_snap = bulk_compare_storage(
        catalog, [StorageRequest(name="vol", capacity_gb=100)]
    )
    with_snap = bulk_compare_storage(
        catalog, [StorageRequest(name="vol", capacity_gb=100, snapshot_count=5)]
    )
    for cloud in ("aws", "azure", "gcp"):
        ssd = catalog.storage_for(cloud, "ssd")
        assert ssd is not None
        expected_extra = round(ssd.snapshot_per_gb_month_usd * 100 * 5, 2)
        delta = round(with_snap["totals_by_cloud"][cloud] - no_snap["totals_by_cloud"][cloud], 2)
        assert delta == expected_extra
    # The "snapshots not priced" note from v0.2 must be gone.
    assert "notes" not in with_snap


def test_compute_os_disk_snapshots_priced():
    catalog = load_catalog()
    no_snap = bulk_compare_compute(
        catalog,
        [ComputeRequest(name="x", vcpus=4, memory_gb=16, os_disk_gb=200)],
    )
    with_snap = bulk_compare_compute(
        catalog,
        [ComputeRequest(name="x", vcpus=4, memory_gb=16, os_disk_gb=200, os_disk_snapshot_count=7)],
    )
    for cloud in ("aws", "azure", "gcp"):
        ssd = catalog.storage_for(cloud, "ssd")
        assert ssd is not None
        expected_extra = round(ssd.snapshot_per_gb_month_usd * 200 * 7, 2)
        delta = round(with_snap["totals_by_cloud"][cloud] - no_snap["totals_by_cloud"][cloud], 2)
        assert delta == expected_extra


def test_commitment_discounts_compute_only():
    catalog = load_catalog()
    compute = [ComputeRequest(name="api", vcpus=4, memory_gb=16, quantity=4)]
    storage = [StorageRequest(name="data", capacity_gb=1000, disk_type="ssd")]
    on_demand = compare_workload(catalog, compute, storage, commitment="none")
    one_year = compare_workload(catalog, compute, storage, commitment="1yr_no_upfront")

    assert "commitment" not in on_demand
    assert "commitment" in one_year
    assert one_year["commitment"]["type"] == "1yr_no_upfront"
    assert one_year["commitment"]["compute_discount_pct"] == 30.0

    discount = COMMITMENT_DISCOUNT["1yr_no_upfront"]
    for cloud in ("aws", "azure", "gcp"):
        compute_od = on_demand["compute"]["totals_by_cloud"][cloud]
        storage_od = on_demand["storage"]["totals_by_cloud"][cloud]
        expected_committed = round(compute_od * (1 - discount) + storage_od, 2)
        actual = one_year["commitment"]["totals_by_cloud"][cloud]
        # Storage is unchanged; compute is reduced by the discount.
        assert abs(actual - expected_committed) < 0.05


def test_three_year_commitment_is_bigger_discount_than_one_year():
    catalog = load_catalog()
    compute = [ComputeRequest(name="api", vcpus=8, memory_gb=32, quantity=10)]
    one_yr = compare_workload(catalog, compute, [], commitment="1yr_no_upfront")
    three_yr = compare_workload(catalog, compute, [], commitment="3yr_partial_upfront")
    for cloud in ("aws", "azure", "gcp"):
        assert (
            three_yr["commitment"]["totals_by_cloud"][cloud]
            < one_yr["commitment"]["totals_by_cloud"][cloud]
        )


def test_summary_includes_annual_savings():
    catalog = load_catalog()
    result = compare_workload(catalog, [ComputeRequest(name="x", vcpus=4, memory_gb=16)], [])
    assert "annual_savings_vs_priciest_usd" in result["combined"]
    monthly = result["combined"]["savings_vs_priciest_usd"]
    annual = result["combined"]["annual_savings_vs_priciest_usd"]
    assert annual == round(monthly * 12, 2)


# --- combined workload ---

def test_compare_workload_sums_compute_plus_storage():
    catalog = load_catalog()
    compute = [ComputeRequest(name="api", vcpus=4, memory_gb=16, quantity=2)]
    storage = [StorageRequest(name="data", capacity_gb=500, disk_type="ssd")]
    combined = compare_workload(catalog, compute, storage)

    assert combined["compute"] is not None
    assert combined["storage"] is not None
    for cloud in ("aws", "azure", "gcp"):
        compute_sub = combined["compute"]["totals_by_cloud"][cloud]
        storage_sub = combined["storage"]["totals_by_cloud"][cloud]
        combined_sub = combined["combined"]["totals_by_cloud"][cloud]
        assert round(combined_sub, 2) == round(compute_sub + storage_sub, 2)


def test_compare_workload_with_only_compute():
    catalog = load_catalog()
    result = compare_workload(catalog, [ComputeRequest(name="x", vcpus=2, memory_gb=4)], [])
    assert result["compute"] is not None
    assert result["storage"] is None
    assert "totals_by_cloud" in result["combined"]


def test_compare_workload_with_only_storage():
    catalog = load_catalog()
    result = compare_workload(catalog, [], [StorageRequest(name="x", capacity_gb=100)])
    assert result["compute"] is None
    assert result["storage"] is not None
    assert "totals_by_cloud" in result["combined"]
