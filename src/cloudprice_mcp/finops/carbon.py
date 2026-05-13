"""compare_carbon_footprint — the only FinOps MCP tool that returns both
cost AND CO2e per workload, side by side.

Model (single workload, single region, conservative defaults):

    instance_power_W = (vcpus * cpu_watts_per_vcpu + memory_gb * memory_watts_per_gb) * utilization_factor
    facility_power_W = instance_power_W * PUE
    monthly_kWh      = facility_power_W * 730 / 1000
    monthly_gCO2e_grid          = monthly_kWh * grid_intensity_g_per_kwh
    monthly_gCO2e_residual      = monthly_gCO2e_grid * (1 - renewable_match_pct/100)
    monthly_gCO2e_market_match  = grid - residual

We report both location-based (grid) and market-based (residual after the
cloud's renewable matching). The market figure is what providers cite; the
grid figure is what an honest auditor wants. Both come back in the result
and we rank clouds by the residual.

Why this is novel: no other public FinOps tool I'm aware of returns kg CO2e
alongside USD on a multi-cloud comparison. AWS / Azure / GCP each publish
their OWN customer dashboards (CCF, Emissions Impact, Cloud Carbon
Footprint) but none of them compare across providers.

Honest gaps (always disclosed in the result):
  - Power model is estimated; actual draw varies with workload utilization
  - Grid intensity is annual average; doesn't reflect time-of-use
  - Renewable matching is annual aggregate, not real-time-matched
  - Embodied carbon (manufacturing the server) is NOT included
  - GPU / network / storage power NOT included in this v1
"""
from __future__ import annotations

import json
from importlib import resources
from typing import Any

from ..compare import compare_all_clouds
from ..pricing import HOURS_PER_MONTH, Cloud, PriceCatalog

ALL_CLOUDS: tuple[Cloud, ...] = ("aws", "azure", "gcp", "oci")

_DATA_PACKAGE = "cloudprice_mcp.data"
_DATA_FILE = "carbon_factors.json"

_factors_cache: dict[str, Any] | None = None


def _load_factors() -> dict[str, Any]:
    global _factors_cache
    if _factors_cache is None:
        text = resources.files(_DATA_PACKAGE).joinpath(_DATA_FILE).read_text(encoding="utf-8")
        _factors_cache = json.loads(text)
    return _factors_cache


def reset_factors_cache() -> None:
    """Used by tests to inject patched factor tables."""
    global _factors_cache
    _factors_cache = None


def compare_carbon_footprint(
    catalog: PriceCatalog,
    vcpus: int,
    memory_gb: float,
    quantity: int = 1,
    targets: list[Cloud] | None = None,
) -> dict[str, Any]:
    """Per-cloud carbon footprint for a vCPU + memory shape, multiplied by
    `quantity` to model a fleet. Returns the same shape regardless of which
    SKU each cloud picked — the comparator handles SKU matching, this tool
    overlays carbon math on top.
    """
    if vcpus < 1:
        raise ValueError("vcpus must be >= 1")
    if memory_gb <= 0:
        raise ValueError("memory_gb must be > 0")
    if quantity < 1:
        raise ValueError("quantity must be >= 1")

    factors = _load_factors()
    clouds = tuple(targets) if targets else ALL_CLOUDS

    matches = [m for m in compare_all_clouds(catalog, vcpus, memory_gb) if m.cloud in clouds]
    per_cloud: list[dict[str, Any]] = []
    for match in matches:
        inst = match.instance
        per_instance = _per_instance_carbon(inst.cloud, inst.sku, inst.vcpus, inst.memory_gb, inst.region, factors)
        # Scale fleet
        fleet_monthly_kwh = per_instance["monthly_kwh"] * quantity
        fleet_monthly_gCO2e_grid = per_instance["monthly_gCO2e_grid"] * quantity
        fleet_monthly_gCO2e_residual = per_instance["monthly_gCO2e_residual"] * quantity
        fleet_monthly_usd = inst.monthly_usd * quantity

        per_cloud.append({
            "cloud": inst.cloud,
            "region": inst.region,
            "sku": inst.sku,
            "vcpus_per_instance": inst.vcpus,
            "memory_gb_per_instance": inst.memory_gb,
            "quantity": quantity,
            "power_class": per_instance["power_class"],
            "instance_watts": round(per_instance["instance_watts"], 2),
            "facility_watts_per_instance": round(per_instance["facility_watts"], 2),
            "pue": per_instance["pue"],
            "grid_intensity_g_per_kwh": per_instance["grid_intensity_g_per_kwh"],
            "renewable_match_pct": per_instance["renewable_match_pct"],
            "monthly_kwh": round(fleet_monthly_kwh, 2),
            "monthly_kg_CO2e_grid": round(fleet_monthly_gCO2e_grid / 1000, 3),
            "monthly_kg_CO2e_residual": round(fleet_monthly_gCO2e_residual / 1000, 3),
            "monthly_usd": round(fleet_monthly_usd, 2),
            "kg_CO2e_per_dollar": round(
                (fleet_monthly_gCO2e_residual / 1000) / fleet_monthly_usd, 4,
            ) if fleet_monthly_usd > 0 else None,
        })

    # Rank by residual (market-based) carbon — that's what gets cited externally.
    per_cloud.sort(key=lambda row: row["monthly_kg_CO2e_residual"])

    recommended = per_cloud[0]["cloud"] if per_cloud else None
    headline = _build_headline(per_cloud, recommended)

    return {
        "kind": "carbon_footprint_comparison",
        "title": f"Carbon footprint: {quantity}x {vcpus} vCPU / {memory_gb} GB",
        "headline": headline,
        "request": {"vcpus": vcpus, "memory_gb": memory_gb, "quantity": quantity},
        "per_cloud": per_cloud,
        "ranking_by_residual_co2e": [r["cloud"] for r in per_cloud],
        "recommended": recommended,
        "recommendation_reason": _build_reason(per_cloud, recommended),
        "honest_gaps": _honest_gaps(),
        "factors_as_of": factors.get("as_of", "unknown"),
    }


def _per_instance_carbon(
    cloud: str, sku: str, vcpus: int, memory_gb: float, region: str, factors: dict[str, Any]
) -> dict[str, Any]:
    """All the per-instance math, factored out for testability."""
    power_model = factors["power_model"]
    is_arm = _is_arm_sku(sku, factors)
    power_class = "arm" if is_arm else "x86"
    cpu_watts_per_vcpu = power_model["arm_watts_per_vcpu" if is_arm else "x86_watts_per_vcpu"]
    memory_watts_per_gb = power_model["memory_watts_per_gb"]
    util = power_model["utilization_factor"]

    instance_watts = (vcpus * cpu_watts_per_vcpu + memory_gb * memory_watts_per_gb) * util

    pue = float(factors["pue_by_cloud"].get(cloud, 1.2))
    facility_watts = instance_watts * pue

    monthly_kwh = facility_watts * HOURS_PER_MONTH / 1000.0

    grid_table = factors["grid_intensity_g_per_kwh_by_region"]
    grid_intensity = float(grid_table.get(region, grid_table.get("_default", 400)))

    monthly_gCO2e_grid = monthly_kwh * grid_intensity

    match_pct = float(factors["renewable_match_pct_by_cloud"].get(cloud, 0))
    monthly_gCO2e_residual = monthly_gCO2e_grid * (1 - match_pct / 100.0)

    return {
        "power_class": power_class,
        "instance_watts": instance_watts,
        "facility_watts": facility_watts,
        "pue": pue,
        "grid_intensity_g_per_kwh": grid_intensity,
        "renewable_match_pct": match_pct,
        "monthly_kwh": monthly_kwh,
        "monthly_gCO2e_grid": monthly_gCO2e_grid,
        "monthly_gCO2e_residual": monthly_gCO2e_residual,
    }


def _is_arm_sku(sku: str, factors: dict[str, Any]) -> bool:
    needles = factors.get("arm_sku_substrings", [])
    return any(needle in sku for needle in needles)


def _build_headline(per_cloud: list[dict], recommended: str | None) -> str:
    if not per_cloud or recommended is None:
        return "No clouds returned a price match for this shape."
    best = per_cloud[0]
    if len(per_cloud) == 1:
        return f"{best['cloud'].upper()} {best['sku']}: {best['monthly_kg_CO2e_residual']} kg CO2e/mo (residual after renewable matching)."
    runner_up = per_cloud[1]
    diff = runner_up["monthly_kg_CO2e_residual"] - best["monthly_kg_CO2e_residual"]
    if best["monthly_kg_CO2e_residual"] > 0:
        pct = diff / best["monthly_kg_CO2e_residual"] * 100 if best["monthly_kg_CO2e_residual"] > 0 else 0
        return (
            f"{best['cloud'].upper()} is lowest carbon at {best['monthly_kg_CO2e_residual']} kg CO2e/mo "
            f"({pct:.0f}% less than the runner-up {runner_up['cloud'].upper()})."
        )
    # All-zero residual = everyone matches 100% renewable. Compare grid-based instead.
    grid_best = min(per_cloud, key=lambda r: r["monthly_kg_CO2e_grid"])
    return (
        f"All providers match 100% renewable (residual = 0). On a location-based "
        f"(grid) measurement, {grid_best['cloud'].upper()} is lowest at {grid_best['monthly_kg_CO2e_grid']} kg CO2e/mo."
    )


def _build_reason(per_cloud: list[dict], recommended: str | None) -> str:
    if not per_cloud or recommended is None:
        return ""
    best = per_cloud[0]
    return (
        f"{best['cloud'].upper()} wins on residual carbon thanks to a combination of "
        f"PUE {best['pue']}, grid intensity {best['grid_intensity_g_per_kwh']} gCO2e/kWh "
        f"in {best['region']}, and {best['renewable_match_pct']}% renewable matching. "
        f"Power class for the matched SKU: {best['power_class']}."
    )


def _honest_gaps() -> list[str]:
    return [
        "Power model is an estimate: (vcpus * watts/vCPU + memory_gb * 0.3W/GB) * 50% utilization. Actual draw varies with workload.",
        "ARM SKUs (Graviton, Ampere, Axion) are estimated at ~30% less per-vCPU power than x86 — surfaced as the `power_class` field.",
        "Grid carbon intensity uses annual averages from public sources (EPA eGRID for US, similar baselines elsewhere). Time-of-use variation is NOT modeled.",
        "Renewable matching is annual aggregate, NOT real-time 24/7 carbon-free energy matching. GCP publishes CFE percentages per region (closest to 24/7).",
        "Embodied carbon (manufacturing the server, the datacenter, the power infrastructure) is NOT included. Operational carbon only.",
        "GPU power, network device power, and storage power are NOT included in this model. Only compute instance + memory.",
        "PUE figures come from each cloud's published sustainability report. Real PUE varies by datacenter, season, and utilization; provider-published averages skew low.",
    ]
