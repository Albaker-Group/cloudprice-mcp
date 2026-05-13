"""Tests for v0.9.0 watch_workload — Cost Drift Sentinel.

Stateless drift sensor. Tests cover the three modes:
  1. Initial baseline capture (no baseline passed)
  2. Drift detection (baseline passed, prices unchanged + prices moved)
  3. Workload-spec change (hash mismatch returns fresh baseline)
"""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops.sentinel import WatchBaseline, watch_workload
from cloudprice_mcp.inventory import parse_dict
from cloudprice_mcp.pricing import (
    reset_catalog_cache,
)


def setup_function():
    reset_catalog_cache()


def _basic_inventory() -> dict:
    return {
        "source_cloud": "aws",
        "compute": [{"name": "web", "vcpus": 4, "memory_gb": 16, "quantity": 6}],
    }


def _other_inventory() -> dict:
    return {
        "source_cloud": "aws",
        "compute": [{"name": "web", "vcpus": 8, "memory_gb": 32, "quantity": 3}],
    }


# --- Mode 1: initial baseline capture ---


def test_no_baseline_returns_watch_baseline_kind():
    from cloudprice_mcp.pricing import load_catalog
    result = watch_workload(load_catalog(), parse_dict(_basic_inventory()))
    assert result["kind"] == "watch_baseline"


def test_initial_baseline_has_per_cloud_costs():
    from cloudprice_mcp.pricing import load_catalog
    result = watch_workload(load_catalog(), parse_dict(_basic_inventory()))
    pc = result["per_cloud_monthly_usd"]
    assert set(pc.keys()) == {"aws", "azure", "gcp", "oci"}
    # AWS + Azure should have non-zero costs for this shape
    assert pc["aws"] > 0
    assert pc["azure"] > 0


def test_initial_baseline_includes_workload_hash():
    from cloudprice_mcp.pricing import load_catalog
    result = watch_workload(load_catalog(), parse_dict(_basic_inventory()))
    h = result["baseline"]["workload_hash"]
    assert isinstance(h, str) and len(h) == 16


def test_workload_hash_is_deterministic():
    from cloudprice_mcp.pricing import load_catalog
    cat = load_catalog()
    r1 = watch_workload(cat, parse_dict(_basic_inventory()))
    r2 = watch_workload(cat, parse_dict(_basic_inventory()))
    assert r1["baseline"]["workload_hash"] == r2["baseline"]["workload_hash"]


def test_different_workloads_have_different_hashes():
    from cloudprice_mcp.pricing import load_catalog
    cat = load_catalog()
    r1 = watch_workload(cat, parse_dict(_basic_inventory()))
    r2 = watch_workload(cat, parse_dict(_other_inventory()))
    assert r1["baseline"]["workload_hash"] != r2["baseline"]["workload_hash"]


# --- Mode 2: drift detection ---


def test_no_drift_when_catalog_unchanged():
    from cloudprice_mcp.pricing import load_catalog
    cat = load_catalog()
    inv = parse_dict(_basic_inventory())
    baseline_resp = watch_workload(cat, inv)
    drift = watch_workload(cat, inv, baseline=baseline_resp["baseline"])
    assert drift["kind"] == "watch_drift_report"
    assert drift["alert_triggered"] is False
    assert drift["max_drift_pct"] == 0.0
    assert drift["recommended_action"] == "no_action"


def test_drift_triggers_alert_above_threshold():
    """Simulate drift by passing a half-priced baseline — current cost is 2x baseline."""
    from cloudprice_mcp.pricing import load_catalog
    cat = load_catalog()
    inv = parse_dict(_basic_inventory())
    baseline_resp = watch_workload(cat, inv)
    tampered = dict(baseline_resp["baseline"])
    tampered["per_cloud_monthly_usd"] = {
        k: v * 0.5 for k, v in tampered["per_cloud_monthly_usd"].items()
    }
    drift = watch_workload(cat, inv, baseline=tampered, alert_threshold_pct=5.0)
    assert drift["alert_triggered"] is True
    assert drift["max_drift_pct"] >= 99.0  # ~100% drift up
    assert drift["recommended_action"] == "investigate"
    assert "ALERT" in drift["headline"]


def test_drift_under_threshold_does_not_alert():
    """Simulate 2% drift while threshold is 5% — should NOT alert."""
    from cloudprice_mcp.pricing import load_catalog
    cat = load_catalog()
    inv = parse_dict(_basic_inventory())
    baseline_resp = watch_workload(cat, inv)
    tampered = dict(baseline_resp["baseline"])
    tampered["per_cloud_monthly_usd"] = {
        k: v / 1.02 for k, v in tampered["per_cloud_monthly_usd"].items()
    }
    drift = watch_workload(cat, inv, baseline=tampered, alert_threshold_pct=5.0)
    assert drift["alert_triggered"] is False
    assert drift["max_drift_pct"] < 5.0


def test_per_cloud_drift_breakdown():
    from cloudprice_mcp.pricing import load_catalog
    cat = load_catalog()
    inv = parse_dict(_basic_inventory())
    baseline_resp = watch_workload(cat, inv)
    drift = watch_workload(cat, inv, baseline=baseline_resp["baseline"])
    clouds_in_report = {row["cloud"] for row in drift["per_cloud"]}
    assert clouds_in_report == {"aws", "azure", "gcp", "oci"}
    for row in drift["per_cloud"]:
        assert "baseline_monthly_usd" in row
        assert "current_monthly_usd" in row
        assert "drift_pct" in row


def test_drift_report_includes_sku_attribution():
    """The bundled history dataset has 2 dated snapshots — sku_attribution
    should surface the OCI -72.78% correction even on a fresh baseline."""
    from cloudprice_mcp.pricing import load_catalog
    cat = load_catalog()
    inv = parse_dict(_basic_inventory())
    # Use a stale "as_of" so the helper walks history before that date.
    baseline = WatchBaseline(
        as_of="2026-04-01",
        catalog_as_of="2026-04-26",
        workload_hash=watch_workload(cat, inv)["baseline"]["workload_hash"],
        per_cloud_monthly_usd={"aws": 700, "azure": 720, "gcp": 0, "oci": 600},
        threshold_pct=5.0,
    )
    drift = watch_workload(cat, inv, baseline=baseline.to_dict())
    skus = drift["sku_attribution"]
    # OCI E5.Flex SKUs should be in the top movers
    oci_e5 = [s for s in skus if s["cloud"] == "oci" and "E5.Flex" in s["sku"]]
    assert len(oci_e5) > 0


# --- Mode 3: workload spec change ---


def test_workload_change_returns_fresh_baseline():
    from cloudprice_mcp.pricing import load_catalog
    cat = load_catalog()
    inv1 = parse_dict(_basic_inventory())
    baseline_resp = watch_workload(cat, inv1)

    inv2 = parse_dict(_other_inventory())
    drift = watch_workload(cat, inv2, baseline=baseline_resp["baseline"])
    assert drift["kind"] == "watch_baseline_replaced"
    assert drift["baseline_replaced"] is True
    assert drift["baseline"]["workload_hash"] != baseline_resp["baseline"]["workload_hash"]


# --- Roundtripping the baseline through to_dict / from_dict ---


def test_baseline_roundtrip_through_dict():
    b = WatchBaseline(
        as_of="2026-05-13",
        catalog_as_of="2026-05-13",
        workload_hash="abc123def456",
        per_cloud_monthly_usd={"aws": 100.0, "azure": 120.5, "gcp": 0.0, "oci": 80.25},
        threshold_pct=7.5,
    )
    d = b.to_dict()
    b2 = WatchBaseline.from_dict(d)
    assert b2 == b


def test_baseline_threshold_pct_defaults_to_5():
    d = {
        "as_of": "2026-05-13",
        "catalog_as_of": "2026-05-13",
        "workload_hash": "abc",
        "per_cloud_monthly_usd": {"aws": 100},
    }
    b = WatchBaseline.from_dict(d)
    assert b.threshold_pct == pytest.approx(5.0)
