from dataclasses import dataclass, field
from typing import Any

from .pricing import (
    HOURS_PER_MONTH,
    Cloud,
    DiskType,
    Instance,
    PriceCatalog,
    StorageSku,
)

CLOUDS: tuple[Cloud, ...] = ("aws", "azure", "gcp")


@dataclass(frozen=True)
class Match:
    cloud: Cloud
    instance: Instance
    spec_distance: float

    def to_dict(self) -> dict:
        return {
            **self.instance.to_dict(),
            "spec_distance": round(self.spec_distance, 3),
        }


@dataclass(frozen=True)
class ComputeRequest:
    name: str
    vcpus: int
    memory_gb: float
    quantity: int = 1
    hours_per_month: int = HOURS_PER_MONTH
    tier: str | None = None
    group: str | None = None
    os_disk_gb: float | None = None
    os_disk_type: DiskType = "ssd"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tier": self.tier,
            "group": self.group,
            "vcpus": self.vcpus,
            "memory_gb": self.memory_gb,
            "quantity": self.quantity,
            "hours_per_month": self.hours_per_month,
            "os_disk_gb": self.os_disk_gb,
            "os_disk_type": self.os_disk_type,
        }


@dataclass(frozen=True)
class StorageRequest:
    name: str
    capacity_gb: float
    disk_type: DiskType = "ssd"
    quantity: int = 1
    tier: str | None = None
    group: str | None = None
    iops: int | None = None
    throughput_mbs: float | None = None
    snapshot_count: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tier": self.tier,
            "group": self.group,
            "capacity_gb": self.capacity_gb,
            "disk_type": self.disk_type,
            "quantity": self.quantity,
            "iops": self.iops,
            "throughput_mbs": self.throughput_mbs,
            "snapshot_count": self.snapshot_count,
        }


# --- single-spec matching (used by v0.1 + v0.2 bulk) ---

def _spec_distance(want_vcpus: int, want_memory_gb: float, candidate: Instance) -> float:
    vcpu_gap = candidate.vcpus - want_vcpus
    mem_gap = candidate.memory_gb - want_memory_gb
    vcpu_penalty = vcpu_gap if vcpu_gap >= 0 else (10 + abs(vcpu_gap))
    mem_penalty = mem_gap if mem_gap >= 0 else (10 + abs(mem_gap))
    return vcpu_penalty + mem_penalty


def best_match(
    catalog: PriceCatalog,
    cloud: Cloud,
    vcpus: int,
    memory_gb: float,
) -> Match | None:
    eligible = [
        c for c in catalog.by_cloud(cloud)
        if c.vcpus >= vcpus and c.memory_gb >= memory_gb
    ]
    candidates = eligible if eligible else list(catalog.by_cloud(cloud))
    if not candidates:
        return None

    scored = [(_spec_distance(vcpus, memory_gb, c), c) for c in candidates]
    scored.sort(key=lambda x: (x[1].hourly_usd, x[0]))
    distance, instance = scored[0]
    return Match(cloud=cloud, instance=instance, spec_distance=distance)


def compare_all_clouds(
    catalog: PriceCatalog,
    vcpus: int,
    memory_gb: float,
) -> list[Match]:
    matches: list[Match] = []
    for cloud in CLOUDS:
        match = best_match(catalog, cloud, vcpus, memory_gb)
        if match is not None:
            matches.append(match)
    matches.sort(key=lambda m: m.instance.monthly_usd)
    return matches


# --- v0.2 bulk + storage compare ---

def _compute_row_cost(
    catalog: PriceCatalog,
    cloud: Cloud,
    req: ComputeRequest,
) -> dict[str, Any] | None:
    match = best_match(catalog, cloud, req.vcpus, req.memory_gb)
    if match is None:
        return None

    instance = match.instance
    hourly_unit_cost = instance.hourly_usd
    compute_unit_monthly = round(hourly_unit_cost * req.hours_per_month, 2)
    compute_total = round(compute_unit_monthly * req.quantity, 2)

    os_disk_total = 0.0
    os_disk_sku: str | None = None
    if req.os_disk_gb and req.os_disk_gb > 0:
        storage = catalog.storage_for(cloud, req.os_disk_type)
        if storage is not None:
            os_disk_unit = round(storage.price_per_gb_month_usd * req.os_disk_gb, 2)
            os_disk_total = round(os_disk_unit * req.quantity, 2)
            os_disk_sku = storage.sku

    return {
        "cloud": cloud,
        "sku": instance.sku,
        "region": instance.region,
        "vcpus": instance.vcpus,
        "memory_gb": instance.memory_gb,
        "hourly_usd": hourly_unit_cost,
        "compute_monthly_per_unit": compute_unit_monthly,
        "compute_monthly_total": compute_total,
        "os_disk_sku": os_disk_sku,
        "os_disk_monthly_total": os_disk_total,
        "row_monthly_total": round(compute_total + os_disk_total, 2),
    }


def _storage_row_cost(
    catalog: PriceCatalog,
    cloud: Cloud,
    req: StorageRequest,
) -> dict[str, Any] | None:
    sku = catalog.storage_for(cloud, req.disk_type)
    if sku is None:
        return None
    unit_monthly = round(sku.price_per_gb_month_usd * req.capacity_gb, 2)
    total_monthly = round(unit_monthly * req.quantity, 2)
    return {
        "cloud": cloud,
        "sku": sku.sku,
        "region": sku.region,
        "disk_type": sku.disk_type,
        "price_per_gb_month_usd": sku.price_per_gb_month_usd,
        "monthly_per_unit": unit_monthly,
        "row_monthly_total": total_monthly,
    }


def _summarize(per_cloud_totals: dict[Cloud, float]) -> dict[str, Any]:
    if not per_cloud_totals:
        return {}
    cheapest = min(per_cloud_totals, key=lambda c: per_cloud_totals[c])
    priciest = max(per_cloud_totals, key=lambda c: per_cloud_totals[c])
    spread = round(per_cloud_totals[priciest] - per_cloud_totals[cheapest], 2)
    pct = (
        round(spread / per_cloud_totals[priciest] * 100, 1)
        if per_cloud_totals[priciest] > 0
        else 0
    )
    return {
        "totals_by_cloud": {c: round(v, 2) for c, v in per_cloud_totals.items()},
        "cheapest_cloud": cheapest,
        "priciest_cloud": priciest,
        "savings_vs_priciest_usd": spread,
        "savings_vs_priciest_pct": pct,
    }


def bulk_compare_compute(
    catalog: PriceCatalog,
    workloads: list[ComputeRequest],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    totals: dict[Cloud, float] = {c: 0.0 for c in CLOUDS}

    for req in workloads:
        per_cloud: dict[str, Any] = {}
        for cloud in CLOUDS:
            cost = _compute_row_cost(catalog, cloud, req)
            if cost is not None:
                per_cloud[cloud] = cost
                totals[cloud] += cost["row_monthly_total"]
        rows.append({"request": req.to_dict(), "per_cloud": per_cloud})

    return {
        "rows": rows,
        **_summarize(totals),
    }


def bulk_compare_storage(
    catalog: PriceCatalog,
    volumes: list[StorageRequest],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    totals: dict[Cloud, float] = {c: 0.0 for c in CLOUDS}
    snapshot_warnings: list[str] = []

    for req in volumes:
        per_cloud: dict[str, Any] = {}
        for cloud in CLOUDS:
            cost = _storage_row_cost(catalog, cloud, req)
            if cost is not None:
                per_cloud[cloud] = cost
                totals[cloud] += cost["row_monthly_total"]
        rows.append({"request": req.to_dict(), "per_cloud": per_cloud})
        if req.snapshot_count > 0:
            snapshot_warnings.append(req.name)

    out: dict[str, Any] = {
        "rows": rows,
        **_summarize(totals),
    }
    if snapshot_warnings:
        out["notes"] = (
            f"Snapshot pricing is not modeled in v0.2 — {len(snapshot_warnings)} "
            "row(s) declared snapshots but their cost is not included in the totals."
        )
    return out


def compare_workload(
    catalog: PriceCatalog,
    compute: list[ComputeRequest],
    storage: list[StorageRequest],
) -> dict[str, Any]:
    compute_result = bulk_compare_compute(catalog, compute) if compute else None
    storage_result = bulk_compare_storage(catalog, storage) if storage else None

    combined: dict[Cloud, float] = {c: 0.0 for c in CLOUDS}
    for sub in (compute_result, storage_result):
        if sub and "totals_by_cloud" in sub:
            for cloud, value in sub["totals_by_cloud"].items():
                combined[cloud] += value

    return {
        "compute": compute_result,
        "storage": storage_result,
        "combined": _summarize(combined) if any(combined.values()) else {},
    }
