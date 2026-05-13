"""compare_spot — multi-cloud spot / preemptible price comparison.

The cross-cloud spot comparison nobody else publishes cleanly:

  - AWS Spot: dynamic price, 2-minute eviction warning, 60-90% discount
  - Azure Spot: fixed-rate discount, eviction by max-price policy
  - GCP Spot VMs: ~60-91% discount, no 24h max (replaced Preemptible VMs)
  - OCI Preemptible: 50% flat discount, 24-hour max lifetime

Each cloud's spot pricing is published in `prices.json` as the
`spot_hourly_usd` field per Instance. This tool finds the best matching SKU
per cloud, computes savings vs the on-demand baseline, and surfaces an
eviction-class label so FinOps practitioners understand what they're trading
off for the discount.
"""
from __future__ import annotations

from typing import Any

from ..compare import Match, compare_all_clouds
from ..pricing import HOURS_PER_MONTH, Cloud, PriceCatalog

ALL_CLOUDS: tuple[Cloud, ...] = ("aws", "azure", "gcp", "oci")

# Eviction characteristics per cloud — sourced from each provider's public
# spot/preemptible documentation. Surfaced in the result so users know what
# they're trading for the discount.
_EVICTION_CLASS: dict[Cloud, dict[str, str]] = {
    "aws": {
        "class": "dynamic_eviction",
        "max_lifetime": "no_max",
        "eviction_notice": "2 minutes",
        "note": "AWS Spot price floats per-AZ; eviction when demand spikes or your max-price is exceeded.",
    },
    "azure": {
        "class": "fixed_rate_eviction",
        "max_lifetime": "no_max",
        "eviction_notice": "30 seconds",
        "note": "Azure Spot uses a published fixed-rate discount; eviction by max-price or capacity pressure.",
    },
    "gcp": {
        "class": "fixed_rate_eviction",
        "max_lifetime": "no_max",
        "eviction_notice": "30 seconds",
        "note": "GCP Spot VMs replaced Preemptible VMs in 2021; same discount tier, no 24-hour max lifetime.",
    },
    "oci": {
        "class": "fixed_rate_eviction",
        "max_lifetime": "24 hours",
        "eviction_notice": "no formal notice; instance terminates",
        "note": "OCI Preemptible Instances are 50% off but capped at 24-hour lifetime — re-launch loop required.",
    },
}


def compare_spot(
    catalog: PriceCatalog,
    vcpus: int,
    memory_gb: float,
    targets: list[Cloud] | None = None,
) -> dict[str, Any]:
    """Cross-cloud spot/preemptible price comparison for a given shape.

    Returns per-cloud best-matching SKU + on-demand cost + spot cost (when
    available) + savings + eviction characteristics, ranked by spot $/hr
    cheapest-first.
    """
    clouds = tuple(targets) if targets else ALL_CLOUDS
    matches = [m for m in compare_all_clouds(catalog, vcpus, memory_gb) if m.cloud in clouds]

    per_cloud: list[dict[str, Any]] = []
    no_spot: list[str] = []

    for match in matches:
        inst = match.instance
        cloud = match.cloud
        if inst.spot_hourly_usd is None:
            no_spot.append(cloud)
            per_cloud.append(_no_spot_row(match))
            continue

        spot_monthly = round(inst.spot_hourly_usd * HOURS_PER_MONTH, 2)
        savings_pct = round((1 - inst.spot_hourly_usd / inst.hourly_usd) * 100, 1) if inst.hourly_usd > 0 else 0.0
        savings_usd_per_month = round(inst.monthly_usd - spot_monthly, 2)
        per_cloud.append({
            "cloud": cloud,
            "sku": inst.sku,
            "region": inst.region,
            "vcpus": inst.vcpus,
            "memory_gb": inst.memory_gb,
            "ondemand_hourly_usd": inst.hourly_usd,
            "ondemand_monthly_usd": inst.monthly_usd,
            "spot_hourly_usd": inst.spot_hourly_usd,
            "spot_monthly_usd": spot_monthly,
            "savings_pct": savings_pct,
            "monthly_savings_usd": savings_usd_per_month,
            "eviction": _EVICTION_CLASS[cloud],
        })

    # Rank cheapest spot first, falling back to on-demand for clouds without spot data.
    def sort_key(row: dict) -> tuple[int, float]:
        spot = row.get("spot_hourly_usd")
        if spot is not None:
            return (0, spot)
        return (1, row.get("ondemand_hourly_usd") or float("inf"))

    per_cloud.sort(key=sort_key)
    ranking = [r["cloud"] for r in per_cloud]
    recommended = ranking[0] if per_cloud else None

    headline = _build_headline(per_cloud, recommended, no_spot)
    honest_gaps = _honest_gaps(no_spot)

    return {
        "kind": "spot_comparison",
        "title": f"Spot pricing comparison: {vcpus} vCPU / {memory_gb} GB",
        "headline": headline,
        "request": {"vcpus": vcpus, "memory_gb": memory_gb},
        "per_cloud": per_cloud,
        "ranking_by_spot_hourly_usd": ranking,
        "recommended": recommended,
        "clouds_without_spot_data": no_spot,
        "honest_gaps": honest_gaps,
    }


def _no_spot_row(match: Match) -> dict[str, Any]:
    inst = match.instance
    return {
        "cloud": match.cloud,
        "sku": inst.sku,
        "region": inst.region,
        "vcpus": inst.vcpus,
        "memory_gb": inst.memory_gb,
        "ondemand_hourly_usd": inst.hourly_usd,
        "ondemand_monthly_usd": inst.monthly_usd,
        "spot_hourly_usd": None,
        "spot_unavailable_reason": (
            "Catalog has no spot price for this SKU. Either the cloud doesn't "
            "offer spot for this instance family (e.g., Azure B-series) or the "
            "weekly refresh hasn't run with credentials yet (e.g., GCP without "
            "GCP_API_KEY set on the refresh workflow)."
        ),
        "eviction": _EVICTION_CLASS[match.cloud],
    }


def _build_headline(per_cloud: list[dict], recommended: str | None, no_spot: list[str]) -> str:
    if recommended is None:
        return "No clouds returned a price match for this shape."
    rec_row = next((r for r in per_cloud if r["cloud"] == recommended), None)
    spot_val = rec_row.get("spot_hourly_usd") if rec_row else None
    if not rec_row or spot_val is None:
        return (
            f"No spot pricing available for any of the {len(per_cloud)} matched clouds. "
            "On-demand-only comparison ranked by hourly cost."
        )
    sku = rec_row["sku"]
    spot = rec_row["spot_hourly_usd"]
    od = rec_row["ondemand_hourly_usd"]
    pct = rec_row["savings_pct"]
    msg = f"{recommended.upper()} {sku} is the cheapest spot at ${spot:.4f}/h (${od:.4f}/h on-demand, {pct}% savings)"
    if no_spot:
        msg += f"; spot data missing for {', '.join(c.upper() for c in no_spot)}"
    return msg + "."


def _honest_gaps(no_spot: list[str]) -> list[str]:
    gaps = [
        "Spot eviction risk differs per cloud — see each row's `eviction` block. OCI Preemptible has a 24-hour max lifetime, the others don't.",
        "AWS Spot is the most price-volatile; the value shown is the recent average across availability zones at the time of the last refresh.",
        "Azure Spot price-floor is configurable per VM; this comparison uses the published current rate.",
        "GCP Spot VMs replaced legacy Preemptible VMs in 2021. We always return Spot rates, not Preemptible.",
        "OCI Preemptible is a flat 50% discount applied to the on-demand rate (not a separately published SKU).",
    ]
    if no_spot:
        gaps.append(
            f"Spot data unavailable for: {', '.join(c.upper() for c in no_spot)}. "
            "Their on-demand prices are shown for context only — don't infer a spot price from on-demand."
        )
    return gaps
