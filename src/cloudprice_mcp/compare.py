from dataclasses import dataclass
from typing import Any, Literal

from .pricing import (
    HOURS_PER_MONTH,
    Cloud,
    DiskType,
    EgressPricing,
    Instance,
    ObjectStorageSku,
    ObjectStorageTier,
    PostgresSku,
    PriceCatalog,
    StorageSku,
)

CLOUDS: tuple[Cloud, ...] = ("aws", "azure", "gcp", "oci")

Commitment = Literal["none", "1yr_no_upfront", "3yr_partial_upfront"]

# Representative compute discount tiers averaged across AWS RI/SP, Azure RI,
# and GCP CUD. Real discount depends on instance family, payment option, and
# region; these are conservative round numbers good for estimation.
COMMITMENT_DISCOUNT: dict[Commitment, float] = {
    "none": 0.0,
    "1yr_no_upfront": 0.30,
    "3yr_partial_upfront": 0.50,
}

# Multi-AZ / HA multipliers — applied to compute totals when multi_az=True.
# These represent the rough cost premium of running active-active across AZs:
#   AWS:  RDS Multi-AZ doubles, EC2 in 2 AZs doubles (sync replicas) → 2.0
#   Azure: zone-redundant zones, often ~1.5-2x. Use 2.0 for parity.
#   GCP:  regional persistent disks + replicas typically ~2.0
#   OCI:  similar HA pairs ~2.0; OCI has fewer nuanced tiers
# Storage stays at 1.0 (most clouds offer redundancy in the storage product itself,
# not as a multiplier; e.g., S3 is already cross-AZ at base price).
MULTI_AZ_COMPUTE_MULTIPLIER: dict[Cloud, float] = {
    "aws": 2.0,
    "azure": 2.0,
    "gcp": 2.0,
    "oci": 2.0,
}


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
    os_disk_snapshot_count: int = 0
    os_disk_snapshot_incremental_factor: float = 1.0

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
            "os_disk_snapshot_count": self.os_disk_snapshot_count,
            "os_disk_snapshot_incremental_factor": self.os_disk_snapshot_incremental_factor,
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
    snapshot_incremental_factor: float = 1.0  # 1.0 = upper-bound, 0.3 = realistic incremental

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
            "snapshot_incremental_factor": self.snapshot_incremental_factor,
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
    os_disk_snapshot_total = 0.0
    os_disk_sku: str | None = None
    if req.os_disk_gb and req.os_disk_gb > 0:
        storage = catalog.storage_for(cloud, req.os_disk_type)
        if storage is not None:
            os_disk_unit = round(storage.price_per_gb_month_usd * req.os_disk_gb, 2)
            os_disk_total = round(os_disk_unit * req.quantity, 2)
            os_disk_sku = storage.sku
            if req.os_disk_snapshot_count > 0:
                os_disk_snapshot_total = storage.snapshot_monthly_cost(
                    capacity_gb=req.os_disk_gb,
                    quantity=req.quantity,
                    snapshot_count=req.os_disk_snapshot_count,
                    incremental_factor=req.os_disk_snapshot_incremental_factor,
                )

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
        "os_disk_snapshot_monthly_total": os_disk_snapshot_total,
        "row_monthly_total": round(compute_total + os_disk_total + os_disk_snapshot_total, 2),
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
    snapshot_total = (
        sku.snapshot_monthly_cost(
            req.capacity_gb,
            req.quantity,
            req.snapshot_count,
            req.snapshot_incremental_factor,
        )
        if req.snapshot_count > 0 else 0.0
    )
    return {
        "cloud": cloud,
        "sku": sku.sku,
        "region": sku.region,
        "disk_type": sku.disk_type,
        "price_per_gb_month_usd": sku.price_per_gb_month_usd,
        "snapshot_per_gb_month_usd": sku.snapshot_per_gb_month_usd,
        "monthly_per_unit": unit_monthly,
        "snapshot_monthly_total": snapshot_total,
        "row_monthly_total": round(total_monthly + snapshot_total, 2),
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
        "annual_savings_vs_priciest_usd": round(spread * 12, 2),
    }


def bulk_compare_compute(
    catalog: PriceCatalog,
    workloads: list[ComputeRequest],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    totals: dict[Cloud, float] = dict.fromkeys(CLOUDS, 0.0)

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
    totals: dict[Cloud, float] = dict.fromkeys(CLOUDS, 0.0)

    for req in volumes:
        per_cloud: dict[str, Any] = {}
        for cloud in CLOUDS:
            cost = _storage_row_cost(catalog, cloud, req)
            if cost is not None:
                per_cloud[cloud] = cost
                totals[cloud] += cost["row_monthly_total"]
        rows.append({"request": req.to_dict(), "per_cloud": per_cloud})

    return {"rows": rows, **_summarize(totals)}


def _build_commitment_section(
    commitment: Commitment,
    combined: dict[Cloud, float],
    compute_result: dict[str, Any] | None,
    storage_result: dict[str, Any] | None,
) -> dict[str, Any]:
    discount = COMMITMENT_DISCOUNT[commitment]
    compute_totals = (
        compute_result["totals_by_cloud"] if compute_result else dict.fromkeys(CLOUDS, 0.0)
    )
    storage_totals = (
        storage_result["totals_by_cloud"] if storage_result else dict.fromkeys(CLOUDS, 0.0)
    )
    committed: dict[Cloud, float] = {
        c: compute_totals.get(c, 0) * (1 - discount) + storage_totals.get(c, 0)
        for c in CLOUDS
    }
    on_demand_priciest = max(combined.values())
    committed_cheapest = min(committed.values())
    return {
        "type": commitment,
        "compute_discount_pct": round(discount * 100, 1),
        "note": (
            "Discount applied to compute only. Storage and snapshots stay at on-demand rates "
            "(most clouds don't offer meaningful storage commitments)."
        ),
        **_summarize(committed),
        "annual_savings_cheapest_vs_on_demand_priciest_usd": round(
            (on_demand_priciest - committed_cheapest) * 12, 2
        ),
    }


@dataclass(frozen=True)
class PostgresRequest:
    name: str
    vcpus: int
    memory_gb: float
    storage_gb: float = 0.0
    quantity: int = 1
    hours_per_month: int = HOURS_PER_MONTH
    tier: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tier": self.tier,
            "vcpus": self.vcpus,
            "memory_gb": self.memory_gb,
            "storage_gb": self.storage_gb,
            "quantity": self.quantity,
            "hours_per_month": self.hours_per_month,
        }


def _postgres_spec_distance(
    want_vcpus: int, want_memory_gb: float, candidate: PostgresSku
) -> float:
    vcpu_gap = candidate.vcpus - want_vcpus
    mem_gap = candidate.memory_gb - want_memory_gb
    vcpu_penalty = vcpu_gap if vcpu_gap >= 0 else (10 + abs(vcpu_gap))
    mem_penalty = mem_gap if mem_gap >= 0 else (10 + abs(mem_gap))
    return vcpu_penalty + mem_penalty


def best_postgres_match(
    catalog: PriceCatalog, cloud: Cloud, vcpus: int, memory_gb: float
) -> PostgresSku | None:
    eligible = [
        p for p in catalog.postgres_by_cloud(cloud)
        if p.vcpus >= vcpus and p.memory_gb >= memory_gb
    ]
    candidates = eligible if eligible else list(catalog.postgres_by_cloud(cloud))
    if not candidates:
        return None
    scored = [(_postgres_spec_distance(vcpus, memory_gb, c), c) for c in candidates]
    scored.sort(key=lambda x: (x[1].hourly_usd, x[0]))
    return scored[0][1]


def _postgres_row_cost(
    catalog: PriceCatalog, cloud: Cloud, req: PostgresRequest
) -> dict[str, Any] | None:
    sku = best_postgres_match(catalog, cloud, req.vcpus, req.memory_gb)
    if sku is None:
        return None
    compute_unit = round(sku.hourly_usd * req.hours_per_month, 2)
    compute_total = round(compute_unit * req.quantity, 2)
    storage_total = round(sku.storage_per_gb_month_usd * req.storage_gb * req.quantity, 2)
    return {
        "cloud": cloud,
        "service": sku.service,
        "sku": sku.sku,
        "region": sku.region,
        "vcpus": sku.vcpus,
        "memory_gb": sku.memory_gb,
        "hourly_usd": sku.hourly_usd,
        "compute_monthly_total": compute_total,
        "storage_monthly_total": storage_total,
        "row_monthly_total": round(compute_total + storage_total, 2),
    }


def compare_postgres(
    catalog: PriceCatalog, requests: list[PostgresRequest]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    totals: dict[Cloud, float] = dict.fromkeys(CLOUDS, 0.0)
    for req in requests:
        per_cloud: dict[str, Any] = {}
        for cloud in CLOUDS:
            cost = _postgres_row_cost(catalog, cloud, req)
            if cost is not None:
                per_cloud[cloud] = cost
                totals[cloud] += cost["row_monthly_total"]
        rows.append({"request": req.to_dict(), "per_cloud": per_cloud})
    return {"rows": rows, **_summarize(totals)}


@dataclass(frozen=True)
class ObjectStorageRequest:
    name: str
    capacity_gb: float
    tier: ObjectStorageTier = "hot"
    quantity: int = 1
    tier_label: str | None = None  # optional grouping

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tier_label": self.tier_label,
            "capacity_gb": self.capacity_gb,
            "tier": self.tier,
            "quantity": self.quantity,
        }


def _best_object_storage_sku(
    catalog: PriceCatalog, cloud: Cloud, tier: ObjectStorageTier, capacity_gb: float
) -> ObjectStorageSku | None:
    """Pick the cheapest SKU on this cloud that matches tier AND can serve capacity.
    Always-Free SKUs (with capacity_gb_limit) only qualify if capacity_gb is within limit."""
    candidates = [
        sku for sku in catalog.object_storage_by_cloud(cloud)
        if sku.tier == tier
        and (sku.capacity_gb_limit is None or capacity_gb <= sku.capacity_gb_limit)
    ]
    if not candidates:
        # Fall back to any tier match without the limit constraint
        candidates = [
            sku for sku in catalog.object_storage_by_cloud(cloud) if sku.tier == tier
        ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.price_per_gb_month_usd)


def _object_storage_row_cost(
    catalog: PriceCatalog, cloud: Cloud, req: ObjectStorageRequest
) -> dict[str, Any] | None:
    sku = _best_object_storage_sku(catalog, cloud, req.tier, req.capacity_gb)
    if sku is None:
        return None
    monthly_total = sku.monthly_cost(req.capacity_gb, req.quantity)
    return {
        "cloud": cloud,
        "service": sku.service,
        "sku": sku.sku,
        "region": sku.region,
        "tier": sku.tier,
        "price_per_gb_month_usd": sku.price_per_gb_month_usd,
        "row_monthly_total": monthly_total,
        "is_free_tier": sku.capacity_gb_limit is not None,
    }


def compare_object_storage(
    catalog: PriceCatalog, requests: list[ObjectStorageRequest]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    totals: dict[Cloud, float] = dict.fromkeys(CLOUDS, 0.0)
    for req in requests:
        per_cloud: dict[str, Any] = {}
        for cloud in CLOUDS:
            cost = _object_storage_row_cost(catalog, cloud, req)
            if cost is not None:
                per_cloud[cloud] = cost
                totals[cloud] += cost["row_monthly_total"]
        rows.append({"request": req.to_dict(), "per_cloud": per_cloud})
    return {"rows": rows, **_summarize(totals)}


@dataclass(frozen=True)
class EgressRequest:
    name: str
    gb_per_month: float
    direction: str = "out_to_internet"  # currently only 'out_to_internet' is modeled
    tier_label: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tier_label": self.tier_label,
            "gb_per_month": self.gb_per_month,
            "direction": self.direction,
        }


def compare_egress(
    catalog: PriceCatalog, requests: list[EgressRequest]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    totals: dict[Cloud, float] = dict.fromkeys(CLOUDS, 0.0)
    for req in requests:
        per_cloud: dict[str, Any] = {}
        for cloud in CLOUDS:
            sku = catalog.egress_for(cloud)
            if sku is None:
                continue
            cost = sku.cost_for_gb(req.gb_per_month)
            per_cloud[cloud] = {
                "cloud": cloud,
                "service": sku.service,
                "region": sku.region,
                "free_tier_gb": sku.free_tier_gb,
                "billed_gb": max(0.0, req.gb_per_month - sku.free_tier_gb),
                "row_monthly_total": cost,
            }
            totals[cloud] += cost
        rows.append({"request": req.to_dict(), "per_cloud": per_cloud})
    return {"rows": rows, **_summarize(totals)}


def _apply_multi_az_to_compute(compute_result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Multiply each cloud's compute totals by its multi-AZ multiplier.
    Returns a NEW result dict (does not mutate the original).
    Per-row totals are also multiplied so callers see consistent numbers."""
    if compute_result is None:
        return None
    new_totals: dict[Cloud, float] = {
        cloud: round(value * MULTI_AZ_COMPUTE_MULTIPLIER.get(cloud, 1.0), 2)
        for cloud, value in compute_result["totals_by_cloud"].items()
    }
    new_rows: list[dict[str, Any]] = []
    for row in compute_result["rows"]:
        new_per_cloud: dict[str, Any] = {}
        for cloud, cost in row["per_cloud"].items():
            mult = MULTI_AZ_COMPUTE_MULTIPLIER.get(cloud, 1.0)
            scaled = dict(cost)
            scaled["compute_monthly_total"] = round(cost["compute_monthly_total"] * mult, 2)
            scaled["row_monthly_total"] = round(cost["row_monthly_total"] * mult, 2)
            new_per_cloud[cloud] = scaled
        new_rows.append({"request": row["request"], "per_cloud": new_per_cloud})
    return {
        "rows": new_rows,
        "multi_az_applied": True,
        "multi_az_multipliers": dict(MULTI_AZ_COMPUTE_MULTIPLIER),
        **_summarize(new_totals),
    }


def compare_workload(
    catalog: PriceCatalog,
    compute: list[ComputeRequest],
    storage: list[StorageRequest],
    commitment: Commitment = "none",
    multi_az: bool = False,
) -> dict[str, Any]:
    compute_result = bulk_compare_compute(catalog, compute) if compute else None
    storage_result = bulk_compare_storage(catalog, storage) if storage else None

    if multi_az:
        compute_result = _apply_multi_az_to_compute(compute_result)

    combined: dict[Cloud, float] = dict.fromkeys(CLOUDS, 0.0)
    for sub in (compute_result, storage_result):
        if sub and "totals_by_cloud" in sub:
            for cloud, value in sub["totals_by_cloud"].items():
                combined[cloud] += value

    out: dict[str, Any] = {
        "compute": compute_result,
        "storage": storage_result,
        "combined": _summarize(combined) if any(combined.values()) else {},
    }
    if multi_az:
        out["multi_az_applied"] = True

    if commitment != "none" and any(combined.values()):
        out["commitment"] = _build_commitment_section(
            commitment, combined, compute_result, storage_result
        )

    return out
