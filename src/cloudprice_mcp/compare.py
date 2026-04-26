from dataclasses import dataclass

from .pricing import Cloud, Instance, PriceCatalog


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


def _spec_distance(want_vcpus: int, want_memory_gb: float, candidate: Instance) -> float:
    """
    Lower is better. Penalizes under-provisioning more than over-provisioning,
    so the picked instance always meets the requested spec.
    """
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
    candidates = [
        c for c in catalog.by_cloud(cloud)
        if c.vcpus >= vcpus and c.memory_gb >= memory_gb
    ]
    if not candidates:
        candidates = list(catalog.by_cloud(cloud))
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
    for cloud in ("aws", "azure", "gcp"):
        match = best_match(catalog, cloud, vcpus, memory_gb)
        if match is not None:
            matches.append(match)
    matches.sort(key=lambda m: m.instance.monthly_usd)
    return matches
