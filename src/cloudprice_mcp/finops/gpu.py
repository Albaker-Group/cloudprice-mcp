"""compare_gpu_workload — cross-cloud GPU pricing comparison.

The fastest-growing cloud cost category in 2026, and nobody compares it
cross-cloud cleanly. AWS, Azure, GCP, and OCI all rent NVIDIA A100 / H100 /
L4 / L40S / A10 / T4 / V100 GPUs, but their packaging differs sharply:
some publish per-GPU rates (Azure NC* with 1 GPU), some bundle 8 GPUs as a
single bare-metal SKU (AWS p4d/p5, OCI BM.GPU.*), and the $/GPU/hour spread
between providers can exceed 3x for the same hardware.

This tool finds the cheapest matching SKU per cloud given a (gpu_type,
gpu_count) request, and ranks clouds by total hourly cost — surfacing both
the absolute cost and the per-GPU cost so users can see whether a cloud
is winning on raw price or on packaging efficiency.

OCI usually wins on H100 / A100 at scale (their BM.GPU.H100.8 = $10/GPU/hr
vs AWS p5.48xlarge = ~$12.29/GPU/hr) — a real moat for AI training workloads
that no commercial FinOps tool I've seen surfaces openly.
"""
from __future__ import annotations

from typing import Any

from ..pricing import Cloud, PriceCatalog

ALL_CLOUDS: tuple[Cloud, ...] = ("aws", "azure", "gcp", "oci")


def compare_gpu_workload(
    catalog: PriceCatalog,
    gpu_type: str,
    gpu_count: int = 1,
    targets: list[Cloud] | None = None,
) -> dict[str, Any]:
    """Find the cheapest SKU per cloud matching (gpu_type, gpu_count).

    For each cloud:
      - Filter instances where gpu_type matches (case-insensitive) and
        gpu_count >= requested. The cheapest such SKU "wins" for that cloud.
      - If no SKU has enough GPUs, that cloud reports no_match.
      - If the matched SKU has MORE GPUs than requested (e.g. user wants
        1 A100 but only an 8-GPU bare-metal SKU is available), the row
        flags over_provisioned=True and reports both the actual SKU cost
        and the prorated per-requested-GPU cost.

    Returns a structured result ranked by hourly cost of the matched SKU,
    plus a per-GPU-hourly ranking for clarity on packaging efficiency.
    """
    if gpu_count < 1:
        raise ValueError("gpu_count must be >= 1")

    clouds = tuple(targets) if targets else ALL_CLOUDS
    needle = gpu_type.upper()

    per_cloud: list[dict[str, Any]] = []
    no_match_clouds: list[str] = []

    for cloud in clouds:
        candidates = [
            inst for inst in catalog.by_cloud(cloud)
            if inst.gpu_count > 0
            and inst.gpu_type
            and inst.gpu_type.upper() == needle
            and inst.gpu_count >= gpu_count
        ]
        if not candidates:
            no_match_clouds.append(cloud)
            continue
        # Cheapest absolute hourly first; tie-broken by smallest GPU count
        # (less over-provisioning).
        cheapest = min(candidates, key=lambda i: (i.hourly_usd, i.gpu_count))
        over_provisioned = cheapest.gpu_count > gpu_count
        prorated_hourly_for_requested = round(
            cheapest.hourly_usd_per_gpu * gpu_count, 4,  # type: ignore[operator]
        ) if cheapest.hourly_usd_per_gpu is not None else None

        per_cloud.append({
            "cloud": cheapest.cloud,
            "sku": cheapest.sku,
            "region": cheapest.region,
            "gpu_type": cheapest.gpu_type,
            "gpu_count_in_sku": cheapest.gpu_count,
            "gpu_count_requested": gpu_count,
            "gpu_memory_gb_each": cheapest.gpu_memory_gb_each,
            "vcpus": cheapest.vcpus,
            "memory_gb": cheapest.memory_gb,
            "hourly_usd": cheapest.hourly_usd,
            "monthly_usd": cheapest.monthly_usd,
            "hourly_usd_per_gpu": cheapest.hourly_usd_per_gpu,
            "prorated_hourly_for_requested": prorated_hourly_for_requested,
            "over_provisioned": over_provisioned,
        })

    per_cloud.sort(key=lambda row: row["hourly_usd"])
    ranking = [r["cloud"] for r in per_cloud]
    recommended = ranking[0] if per_cloud else None
    per_gpu_ranking = sorted(per_cloud, key=lambda r: r["hourly_usd_per_gpu"])
    per_gpu_winner = per_gpu_ranking[0] if per_gpu_ranking else None

    headline = _build_headline(per_cloud, per_gpu_winner, gpu_type, gpu_count, no_match_clouds)

    return {
        "kind": "gpu_workload_comparison",
        "title": f"GPU workload: {gpu_count}x {gpu_type}",
        "headline": headline,
        "request": {"gpu_type": gpu_type, "gpu_count": gpu_count},
        "per_cloud": per_cloud,
        "ranking_by_total_hourly": ranking,
        "ranking_by_per_gpu_hourly": [r["cloud"] for r in per_gpu_ranking],
        "recommended": recommended,
        "per_gpu_hourly_winner": per_gpu_winner["cloud"] if per_gpu_winner else None,
        "clouds_without_match": no_match_clouds,
        "honest_gaps": _honest_gaps(no_match_clouds, per_cloud),
    }


def _build_headline(
    per_cloud: list[dict],
    per_gpu_winner: dict | None,
    gpu_type: str,
    gpu_count: int,
    no_match: list[str],
) -> str:
    if not per_cloud:
        return f"No cloud in the catalog publishes an {gpu_type} SKU with at least {gpu_count} GPU(s)."

    cheapest = per_cloud[0]
    msg = (
        f"{cheapest['cloud'].upper()} {cheapest['sku']} is cheapest at "
        f"${cheapest['hourly_usd']:.4f}/h for {gpu_count}x {gpu_type}"
    )
    if cheapest["over_provisioned"]:
        msg += f" (over-provisioned — SKU has {cheapest['gpu_count_in_sku']} GPUs)"
    msg += "."

    # Surface the per-GPU efficiency winner separately if different from absolute.
    if per_gpu_winner and per_gpu_winner["cloud"] != cheapest["cloud"]:
        msg += (
            f" Best $/GPU/h: {per_gpu_winner['cloud'].upper()} "
            f"{per_gpu_winner['sku']} at ${per_gpu_winner['hourly_usd_per_gpu']:.4f}/GPU/h."
        )

    if no_match:
        msg += f" No matching SKU on: {', '.join(c.upper() for c in no_match)}."

    return msg


def _honest_gaps(no_match_clouds: list[str], per_cloud: list[dict]) -> list[str]:
    gaps = [
        "GPU prices are on-demand list — Spot / Preemptible discounts (60-90% off) are NOT applied here. Use compare_spot separately for GPU spot rates where available.",
        "Bare-metal SKUs are sold as a single fixed unit (e.g. 8x A100). Asking for fewer GPUs than the SKU provides flags `over_provisioned=True` — you pay for the whole SKU.",
        "GPU memory differs by variant (A100 40GB vs A100 80GB; same `gpu_type` field). Check `gpu_memory_gb_each` per row before signing off.",
        "Catalog tracks a representative slice of GPU SKUs per cloud — niche shapes (V100 8x, A10G multi-GPU, MI300, TPU v5p) are not yet included.",
    ]
    if any(r["over_provisioned"] for r in per_cloud):
        gaps.append(
            "At least one cloud's only matching SKU is over-provisioned. Real cost is the full SKU price, not the prorated figure (which is shown for relative-efficiency comparison only)."
        )
    if no_match_clouds:
        gaps.append(
            f"No GPU SKU in this catalog matches the request for: {', '.join(c.upper() for c in no_match_clouds)}. "
            f"This may mean the cloud doesn't publish that GPU type, or we haven't curated it yet."
        )
    return gaps
