"""Tests for v0.6 caveats library."""
from __future__ import annotations

from cloudprice_mcp import caveats
from cloudprice_mcp.caveats import (
    Caveat,
    evaluate,
    has_blocker,
    load_caveats,
    reset_caveat_cache,
    split_by_severity,
)
from cloudprice_mcp.inventory import (
    ComputeItem,
    EgressItem,
    OneTime,
    StorageItem,
    WorkloadInventory,
)


def setup_function():
    reset_caveat_cache()


# --- Loading ---


def test_load_caveats_returns_at_least_basic_set():
    out = load_caveats()
    assert len(out) >= 5  # we ship 6 in caveats.json
    assert all(isinstance(c, Caveat) for c in out)
    ids = {c.id for c in out}
    # Sanity check on the ones we know we shipped
    assert "source_equals_target" in ids
    assert "oci_a1_arm_workload" in ids


def test_load_caveats_is_cached():
    first = load_caveats()
    second = load_caveats()
    assert first is second  # same list object


# --- Trigger: source_equals_target (block) ---


def test_source_equals_target_triggers_block_caveat():
    inv = WorkloadInventory(source_cloud="aws", compute=[
        ComputeItem(name="x", vcpus=2, memory_gb=4)
    ])
    triggered = evaluate(inv, target_cloud="aws")
    ids = {c.id for c in triggered}
    assert "source_equals_target" in ids
    blocker = next(c for c in triggered if c.id == "source_equals_target")
    assert blocker.severity == "block"


def test_source_not_equal_target_no_block():
    inv = WorkloadInventory(source_cloud="aws", compute=[
        ComputeItem(name="x", vcpus=2, memory_gb=4)
    ])
    triggered = evaluate(inv, target_cloud="oci")
    blockers = [c for c in triggered if c.severity == "block"]
    assert blockers == []


# --- Trigger: oci_a1_arm_workload (warn, OCI-only) ---


def test_oci_a1_warn_triggers_when_compute_fits_a1_and_target_is_oci():
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="api", vcpus=4, memory_gb=16)],  # fits A1.Flex
    )
    triggered = evaluate(inv, target_cloud="oci")
    ids = {c.id for c in triggered}
    assert "oci_a1_arm_workload" in ids


def test_oci_a1_warn_does_not_trigger_for_other_targets():
    inv = WorkloadInventory(
        source_cloud="oci",
        compute=[ComputeItem(name="api", vcpus=4, memory_gb=16)],
    )
    triggered = evaluate(inv, target_cloud="aws")
    ids = {c.id for c in triggered}
    assert "oci_a1_arm_workload" not in ids


def test_oci_a1_warn_does_not_trigger_when_compute_exceeds_a1_limits():
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="big", vcpus=16, memory_gb=64)],  # too big for A1
    )
    triggered = evaluate(inv, target_cloud="oci")
    ids = {c.id for c in triggered}
    assert "oci_a1_arm_workload" not in ids


# --- Trigger: commitment_3yr_not_portable (info) ---


def test_3yr_commitment_triggers_info_caveat():
    inv = WorkloadInventory(
        source_cloud="aws",
        commitment="3yr_partial_upfront",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "commitment_3yr_not_portable" in ids


def test_1yr_commitment_does_not_trigger_3yr_caveat():
    inv = WorkloadInventory(
        source_cloud="aws",
        commitment="1yr_no_upfront",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "commitment_3yr_not_portable" not in ids


def test_no_commitment_does_not_trigger_3yr_caveat():
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "commitment_3yr_not_portable" not in ids


# --- Trigger: high_egress_warrants_dx (info) ---


def test_high_egress_triggers_dx_caveat():
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="cdn", gb_per_month=200_000)],  # 200 TB
    )
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "high_egress_warrants_dx" in ids


def test_low_egress_does_not_trigger_dx_caveat():
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(name="api", gb_per_month=5000)],  # 5 TB
    )
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "high_egress_warrants_dx" not in ids


def test_inter_region_egress_does_not_count_toward_internet_threshold():
    inv = WorkloadInventory(
        source_cloud="aws",
        egress=[EgressItem(
            name="repl", gb_per_month=200_000, direction="inter_region"
        )],
    )
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "high_egress_warrants_dx" not in ids


# --- Trigger: multi_az_doubles_compute (info) ---


def test_multi_az_triggers_info_caveat():
    inv = WorkloadInventory(
        source_cloud="aws", multi_az=True,
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "multi_az_doubles_compute" in ids


def test_multi_az_off_does_not_trigger():
    inv = WorkloadInventory(
        source_cloud="aws", multi_az=False,
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "multi_az_doubles_compute" not in ids


# --- Trigger: no_compute_workload (info) ---


def test_no_compute_workload_triggers_when_only_storage():
    inv = WorkloadInventory(
        source_cloud="aws",
        storage=[StorageItem(name="x", capacity_gb=100)],  # storage only
    )
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "no_compute_workload" in ids


def test_no_compute_workload_does_not_trigger_when_compute_present():
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "no_compute_workload" not in ids


def test_no_compute_workload_does_not_trigger_when_inventory_completely_empty():
    """An entirely empty inventory shouldn't double-flag — only flag when the user
    has SOMETHING but specifically forgot compute."""
    inv = WorkloadInventory(source_cloud="aws")
    triggered = evaluate(inv, target_cloud="azure")
    ids = {c.id for c in triggered}
    assert "no_compute_workload" not in ids


# --- Ordering + utility helpers ---


def test_evaluate_returns_caveats_sorted_by_severity():
    inv = WorkloadInventory(
        source_cloud="aws",
        commitment="3yr_partial_upfront",  # info caveat
        multi_az=True,                      # info caveat
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
        egress=[EgressItem(name="cdn", gb_per_month=200_000)],  # info caveat
    )
    # Target = source → block. Plus the infos.
    triggered = evaluate(inv, target_cloud="aws")
    severities = [c.severity for c in triggered]
    # Block must come first
    assert severities[0] == "block"
    # No 'warn' present, all rest should be 'info'
    assert all(s == "info" for s in severities[1:])


def test_split_by_severity_groups_correctly():
    inv = WorkloadInventory(
        source_cloud="aws",
        commitment="3yr_partial_upfront",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    triggered = evaluate(inv, target_cloud="aws")  # block + info
    grouped = split_by_severity(triggered)
    assert len(grouped["block"]) == 1
    assert len(grouped["info"]) >= 1
    assert grouped["warn"] == []


def test_has_blocker_true_when_source_equals_target():
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    triggered = evaluate(inv, target_cloud="aws")
    assert has_blocker(triggered)


def test_has_blocker_false_when_no_block_severity():
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    triggered = evaluate(inv, target_cloud="azure")
    assert not has_blocker(triggered)


# --- Cloud filter ---


def test_caveat_with_specific_cloud_filter_only_triggers_on_that_cloud():
    """oci_a1_arm_workload has cloud='oci'; must not trigger when target=azure."""
    inv = WorkloadInventory(
        source_cloud="aws",
        compute=[ComputeItem(name="x", vcpus=2, memory_gb=4)],
    )
    azure_caveats = evaluate(inv, target_cloud="azure")
    azure_ids = {c.id for c in azure_caveats}
    assert "oci_a1_arm_workload" not in azure_ids


def test_evaluate_skips_unknown_trigger_id_gracefully(monkeypatch):
    """Adding a future caveat with an unknown trigger_id shouldn't crash."""
    # Inject a synthetic caveat referencing a trigger that doesn't exist
    fake = Caveat(
        id="future_caveat",
        cloud="any",
        trigger_id="not_a_real_trigger",
        message="from the future",
        severity="info",
    )
    real = load_caveats()
    monkeypatch.setattr(caveats, "_caveat_cache", real + [fake])
    inv = WorkloadInventory(source_cloud="aws", compute=[
        ComputeItem(name="x", vcpus=2, memory_gb=4)
    ])
    # Should not raise
    triggered = evaluate(inv, target_cloud="azure")
    assert "future_caveat" not in {c.id for c in triggered}
