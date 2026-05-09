"""Tests for v0.6 finops/migration.py — assess_migration orchestrator."""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops.migration import assess_migration
from cloudprice_mcp.inventory import (
    ComputeItem,
    EgressItem,
    ObjectStorageItem,
    OneTime,
    StorageItem,
    WorkloadInventory,
)
from cloudprice_mcp.pricing import load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


# --- Input validation ---


def test_assess_migration_requires_source_cloud():
    inv = WorkloadInventory(
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)]
    )
    catalog = load_catalog()
    with pytest.raises(ValueError, match="source_cloud must be set"):
        assess_migration(catalog, inv)


def test_assess_migration_rejects_empty_inventory():
    catalog = load_catalog()
    inv = WorkloadInventory(source_cloud="aws")  # nothing in any section
    with pytest.raises(ValueError, match="no workload items"):
        assess_migration(catalog, inv)


# --- Output shape ---


def test_result_has_canonical_shape():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="api", vcpus=4, memory_gb=16, quantity=2)],
    )
    result = assess_migration(catalog, inv)
    # Required top-level keys
    assert result["kind"] == "migration_assessment"
    assert "title" in result
    assert "headline" in result
    assert result["source_cloud"] == "aws"
    assert isinstance(result["source_monthly_usd"], (int, float))
    assert "targets" in result
    assert isinstance(result["one_time_exit_cost_usd"], (int, float))
    assert isinstance(result["ranking_by_3yr_tco"], list)
    assert "honest_gaps" in result
    assert isinstance(result["honest_gaps"], list)


def test_default_targets_excludes_source_cloud():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    result = assess_migration(catalog, inv)
    assert "aws" not in result["targets"]
    assert set(result["targets"].keys()) == {"azure", "gcp", "oci"}


def test_explicit_targets_respected():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    result = assess_migration(catalog, inv, targets=["oci"])
    assert set(result["targets"].keys()) == {"oci"}


def test_per_target_dict_has_required_fields():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=4, memory_gb=16)],
    )
    result = assess_migration(catalog, inv)
    for cloud, t in result["targets"].items():
        assert "monthly_usd" in t, f"missing monthly_usd on {cloud}"
        assert "savings_vs_source_pct" in t
        assert "payback_months" in t  # may be None
        assert "three_year_total_usd" in t
        assert "caveats" in t and isinstance(t["caveats"], list)
        assert "blockers" in t and isinstance(t["blockers"], list)


# --- Cost computation correctness ---


def test_target_cost_includes_object_storage_when_specified():
    catalog = load_catalog()
    base = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    augmented = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
        object_storage=[
            ObjectStorageItem(name="media", capacity_gb=10000, tier="hot")
        ],
    )
    base_result = assess_migration(catalog, base)
    aug_result = assess_migration(catalog, augmented)
    # Adding 10 TB of object storage should make every target cost more
    for cloud in base_result["targets"]:
        assert (
            aug_result["targets"][cloud]["monthly_usd"]
            >= base_result["targets"][cloud]["monthly_usd"]
        )


def test_target_cost_includes_internet_egress():
    catalog = load_catalog()
    base = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    with_egress = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
        egress=[EgressItem(name="cdn", gb_per_month=50_000)],  # 50 TB
    )
    # Use same source so source_monthly differs only by egress
    base_result = assess_migration(catalog, base)
    egress_result = assess_migration(catalog, with_egress)
    # Source should now be more expensive (AWS internet egress at 50TB)
    assert egress_result["source_monthly_usd"] > base_result["source_monthly_usd"]
    # OCI should still be cheaper than AWS for egress (the 12× moat)
    assert (
        egress_result["targets"]["oci"]["monthly_usd"]
        < egress_result["source_monthly_usd"]
    )


def test_target_cost_includes_inter_region_egress():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
        egress=[EgressItem(
            name="repl", gb_per_month=10_000, direction="inter_region"
        )],
    )
    result = assess_migration(catalog, inv)
    # Each target should have a non-zero monthly that includes inter-region
    for cloud, t in result["targets"].items():
        # 10 TB inter-region at $0.0085-$0.02 per GB → at least $85
        assert t["monthly_usd"] > 80


# --- Exit cost + payback ---


def test_exit_cost_zero_when_no_data_to_migrate():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    result = assess_migration(catalog, inv)
    assert result["one_time_exit_cost_usd"] == 0


def test_exit_cost_calculated_from_source_egress():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
        one_time=OneTime(data_to_migrate_gb=10_000),  # 10 TB out of AWS
    )
    result = assess_migration(catalog, inv)
    # AWS egress: 100 GB free, then $0.09/GB tier 1 → ~9900 GB × $0.09 ≈ $891
    assert result["one_time_exit_cost_usd"] > 800
    assert result["one_time_exit_cost_usd"] < 1000


def test_payback_months_calculated_when_target_cheaper():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="api", vcpus=8, memory_gb=32, quantity=4)],
        one_time=OneTime(data_to_migrate_gb=5000),
    )
    result = assess_migration(catalog, inv)
    # OCI should be cheaper, so payback should be a positive number
    oci = result["targets"]["oci"]
    if oci["savings_vs_source_pct"] > 0 and oci["payback_months"] is not None:
        assert oci["payback_months"] > 0


def test_payback_zero_when_target_cheaper_no_exit_cost():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="api", vcpus=8, memory_gb=32, quantity=4)],
        # no one_time data_to_migrate
    )
    result = assess_migration(catalog, inv)
    # Any target that's cheaper should have payback_months=0 (immediate)
    for t in result["targets"].values():
        if t["savings_vs_source_pct"] > 0:
            assert t["payback_months"] == 0


def test_payback_none_when_target_more_expensive():
    """If a target is more expensive than the source, payback is null (never pays back)."""
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
        one_time=OneTime(data_to_migrate_gb=10_000),
    )
    result = assess_migration(catalog, inv)
    # At least one target may be more expensive than source — verify the rule
    for t in result["targets"].values():
        if t["savings_vs_source_pct"] <= 0:
            assert t["payback_months"] is None


# --- Ranking + recommendation ---


def test_ranking_orders_by_3yr_tco_ascending():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=4, memory_gb=16, quantity=2)],
    )
    result = assess_migration(catalog, inv)
    ranking = result["ranking_by_3yr_tco"]
    totals = [result["targets"][c]["three_year_total_usd"] for c in ranking]
    assert totals == sorted(totals)


def test_recommended_is_first_in_ranking():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=4, memory_gb=16)],
    )
    result = assess_migration(catalog, inv)
    if result["ranking_by_3yr_tco"]:
        assert result["recommended"] == result["ranking_by_3yr_tco"][0]


# --- Caveats integration ---


def test_oci_target_includes_arm_caveat_when_compute_fits_a1():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=4, memory_gb=16)],  # fits A1.Flex
    )
    result = assess_migration(catalog, inv)
    oci_caveats = " ".join(result["targets"]["oci"]["caveats"]).lower()
    assert "arm" in oci_caveats


def test_3yr_commitment_appears_in_info_lists():
    catalog = load_catalog()
    inv = WorkloadInventory(
        source_cloud="aws",
        commitment="3yr_partial_upfront",
        compute=[ComputeItem(name="x", vcpus=4, memory_gb=16)],
    )
    result = assess_migration(catalog, inv)
    azure_info = " ".join(result["targets"]["azure"]["info"]).lower()
    assert "3-year" in azure_info or "3yr" in azure_info or "not portable" in azure_info


# --- Multi-AZ propagates ---


def test_multi_az_increases_target_cost():
    """Use a workload too big for OCI A1.Flex Always Free, so every cloud has
    non-zero compute cost that multi-AZ can double."""
    catalog = load_catalog()
    base = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=16, memory_gb=64, quantity=2)],
    )
    multi = WorkloadInventory(
        source_cloud="aws",
        multi_az=True,
        compute=[ComputeItem(name="x", vcpus=16, memory_gb=64, quantity=2)],
    )
    base_result = assess_migration(catalog, base)
    multi_result = assess_migration(catalog, multi)
    for cloud in base_result["targets"]:
        base_cost = base_result["targets"][cloud]["monthly_usd"]
        multi_cost = multi_result["targets"][cloud]["monthly_usd"]
        # Multi-AZ doubles compute on every cloud, so target cost must roughly double
        assert multi_cost > base_cost, (
            f"{cloud}: multi-az ({multi_cost}) should exceed base ({base_cost})"
        )
