"""
`find_egress_arbitrage` — the "where do I save on data transfer?" FinOps tool.

A specialized assess_migration scoped to egress patterns only. Useful when a
team's largest cost line is data transfer (CDN workloads, video streaming,
content distribution) and they want to know specifically whether moving the
egress-heavy portion of their workload to a cheaper provider is worth it.

The OCI 12× moat is the headline finding for most egress-heavy workloads:
at 50 TB/month internet egress, OCI is roughly $340 vs $4,000+ on the
hyperscalers, because of OCI's 10 TB/month free tier + $0.0085/GB beyond.

Scope for v0.6.0:
- Internet egress (out_to_internet) — handled by compare_egress with free-tier logic
- Inter-region egress — handled by EgressPricing.inter_region_cost_for_gb
- One-time exit cost from source cloud (optional, via inventory.one_time)
- Annual savings projection
- Payback period when exit cost is non-zero

Out of scope:
- Egress to other VPCs / VPC peering (not modeled in v0.5)
- Cloudflare / Fastly / dedicated CDN providers (different category)
- Per-region pricing (waits for v0.7+ multi-region support)
"""
from __future__ import annotations

from ..compare import CLOUDS, EgressRequest, compare_egress
from ..inventory import WorkloadInventory
from ..pricing import Cloud, PriceCatalog


def find_egress_arbitrage(
    catalog: PriceCatalog,
    inventory: WorkloadInventory,
    targets: list[Cloud] | None = None,
) -> dict:
    """Project per-target egress cost + payback for moving egress workloads.

    Args:
        catalog: pricing catalog (bundled or live)
        inventory: workload inventory — only `source_cloud`, `egress`, and `one_time`
                   are used by this tool. Other sections are ignored.
        targets: clouds to evaluate (default: all 4 clouds except source)

    Returns:
        dict with `kind=egress_arbitrage`, per-target breakdown, recommendation.
    """
    if not inventory.source_cloud:
        raise ValueError(
            "find_egress_arbitrage: inventory.source_cloud must be set."
        )
    if not inventory.egress:
        raise ValueError(
            "find_egress_arbitrage: inventory.egress must contain at least one item."
        )

    source = inventory.source_cloud
    target_clouds: list[Cloud] = list(targets) if targets else [
        c for c in CLOUDS if c != source
    ]
    if not target_clouds:
        raise ValueError(
            "find_egress_arbitrage: no target clouds to evaluate."
        )

    source_monthly = _egress_total_for_cloud(catalog, inventory, source)
    annual_source_usd = source_monthly * 12
    exit_cost = _exit_cost(catalog, inventory)

    per_target: dict[str, dict] = {}
    for target in target_clouds:
        target_monthly = _egress_total_for_cloud(catalog, inventory, target)
        monthly_savings = source_monthly - target_monthly
        annual_savings = monthly_savings * 12
        savings_pct = (
            round(100 * monthly_savings / source_monthly)
            if source_monthly > 0
            else 0
        )
        if monthly_savings > 0 and exit_cost > 0:
            payback = round(exit_cost / monthly_savings, 1)
        elif monthly_savings > 0:
            payback = 0.0
        else:
            payback = None

        per_target[target] = {
            "monthly_usd": round(target_monthly, 2),
            "monthly_savings_usd": round(monthly_savings, 2),
            "annual_savings_usd": round(annual_savings, 2),
            "savings_vs_source_pct": savings_pct,
            "payback_months": payback,
            "three_year_savings_usd": round(annual_savings * 3 - exit_cost, 2),
        }

    rankable = [(name, t) for name, t in per_target.items() if t["monthly_savings_usd"] > 0]
    rankable.sort(key=lambda x: x[1]["three_year_savings_usd"], reverse=True)
    ranking = [name for name, _ in rankable]
    recommended = ranking[0] if ranking else None

    headline = _build_headline(source, source_monthly, recommended, per_target, exit_cost)
    reason = (
        _build_reason(recommended, per_target, exit_cost)
        if recommended
        else "No target offers savings on this egress profile."
    )

    return {
        "kind": "egress_arbitrage",
        "title": f"Egress Arbitrage: {source.upper()}",
        "headline": headline,
        "source_cloud": source,
        "source_monthly_usd": round(source_monthly, 2),
        "annual_source_usd": round(annual_source_usd, 2),
        "one_time_exit_cost_usd": round(exit_cost, 2),
        "targets": per_target,
        "ranking_by_3yr_savings": ranking,
        "recommended": recommended,
        "recommendation_reason": reason,
        "honest_gaps": [
            "VPC peering / Direct Connect / dedicated CDN providers (Cloudflare, Fastly) not modeled",
            "Single-region pricing only — multi-region egress patterns may differ",
            "Assumes egress profile stays constant; growth modeling is in compare_total_cost_of_ownership",
            "If you only move egress (not workload), inter-AZ traffic to your origin still applies",
        ],
    }


# --- Per-cloud egress total ---


def _egress_total_for_cloud(
    catalog: PriceCatalog, inv: WorkloadInventory, cloud: Cloud
) -> float:
    """Total monthly egress cost on `cloud` — internet + inter-region combined."""
    total = 0.0

    # Internet egress (uses compare_egress with free-tier logic)
    internet = [e for e in inv.egress if e.direction == "out_to_internet"]
    if internet:
        reqs = [
            EgressRequest(name=e.name, gb_per_month=e.gb_per_month, direction=e.direction)
            for e in internet
        ]
        result = compare_egress(catalog, reqs)
        total += result["totals_by_cloud"].get(cloud, 0)

    # Inter-region egress (flat per-cloud rate)
    inter_region = [e for e in inv.egress if e.direction == "inter_region"]
    if inter_region:
        sku = catalog.egress_for(cloud)
        if sku is not None:
            for e in inter_region:
                total += sku.inter_region_cost_for_gb(e.gb_per_month)

    return total


def _exit_cost(catalog: PriceCatalog, inv: WorkloadInventory) -> float:
    """One-time egress out of source cloud for inventory.one_time.data_to_migrate_gb."""
    if inv.one_time.data_to_migrate_gb <= 0 or inv.source_cloud is None:
        return 0.0
    sku = catalog.egress_for(inv.source_cloud)
    if sku is None:
        return 0.0
    return sku.cost_for_gb(inv.one_time.data_to_migrate_gb)


# --- Headlines ---


def _build_headline(
    source: str,
    source_monthly: float,
    recommended: str | None,
    per_target: dict[str, dict],
    exit_cost: float,
) -> str:
    if recommended is None:
        return (
            f"No target saves on this egress profile vs {source.upper()} "
            f"(${source_monthly:,.0f}/mo)."
        )
    rec = per_target[recommended]
    annual = rec["annual_savings_usd"]
    payback = rec["payback_months"]
    if payback is not None and payback > 0:
        return (
            f"{recommended.upper()} saves ${annual:,.0f}/yr after a {payback}-month "
            f"payback on ${exit_cost:,.0f} exit cost."
        )
    return f"{recommended.upper()} saves ${annual:,.0f}/yr (no exit cost)."


def _build_reason(
    recommended: str,
    per_target: dict[str, dict],
    exit_cost: float,
) -> str:
    rec = per_target[recommended]
    annual = rec["annual_savings_usd"]
    three_yr = rec["three_year_savings_usd"]
    pct = rec["savings_vs_source_pct"]
    if exit_cost > 0:
        return (
            f"{pct}% cheaper monthly. ${annual:,.0f}/yr ongoing savings; "
            f"${three_yr:,.0f} net over 3 years after ${exit_cost:,.0f} exit cost."
        )
    return f"{pct}% cheaper monthly. ${annual:,.0f}/yr saved with no exit cost."
