"""reprice_bill — the compute on your actual bill, priced on all four clouds.

The comparison tools start from a spec someone types in. This one starts from a
FOCUS export: it works out the vCPU-hours and GiB-hours the bill's compute
actually used, then prices that same capacity at list price on AWS, Azure, GCP
and OCI from the bundled catalogue.

How a row becomes capacity:
  - Instance rows (how AWS and Azure bill VMs): hours x the instance's shape.
    The shape comes from SkuPriceDetails CoreCount/MemorySize (FOCUS 1.1+), else
    the catalogue, else the instance name (m6i.large -> 2 vCPU, 8 GiB).
  - vCPU and memory rows (how GCP and OCI bill VMs, and how Fargate and other
    container services bill): summed per resource, then paired.

How capacity is priced on a cloud: the catalogue shape that runs it cheapest,
as instance-hours = max(vCPU-hours / vCPUs, GiB-hours / GiB). GPUs are never
targets; burstable shapes are offered only to burstable workloads, ARM only to
ARM workloads.

Same privacy rules as analyze_bill: local file, no network, local-only tool.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..compare import CLOUDS
from ..pricing import Cloud, Instance, PriceCatalog
from .bill_insights import _CLOUD_OF, _load
from .focus_ingest import BillError, _num, row_cost, usage_kind

# Columns that name what was used, in the order they are trusted. FOCUS has no
# instance-type column; providers put it in their own (x_) columns or the text.
_TEXT_COLUMNS = (
    "x_UsageType",  # AWS: BoxUsage:m5.large
    "SkuMeter",  # FOCUS 1.1+, Azure: D2s v5
    "x_SkuMeterName",
    "x_SkuDescription",
    "ChargeDescription",
    "x_SkuMeterSubcategory",
)
_ASSUMED_GIB_PER_VCPU = 4.0  # general purpose, for vCPU rows with no memory row

_LICENSED_OS = re.compile(
    r"\b(windows|rhel|red hat|suse|sles|sql server|sql (std|standard|enterprise|web))\b", re.I
)
_GPU = re.compile(r"\bgpus?\b", re.I)
_CPU = re.compile(r"\b(v?cpus?|cores?|ocpus?)\b", re.I)
_ARM_TEXT = re.compile(r"\b(a1|a2|ampere|graviton|arm|arm64|t2a|c4a)\b", re.I)

# Target shapes a workload may move to, by SKU name.
_BURSTABLE = {
    "aws": re.compile(r"^t\d"),
    "azure": re.compile(r"^(standard_)?b\d"),
    "gcp": re.compile(r"^(e2-(micro|small|medium)|f1-micro|g1-small)$"),
    "oci": re.compile(r"\.micro$"),
}
_ARM = {
    "aws": re.compile(r"^[a-z]+\d+[a-z]*g"),  # m6g, c7gn, t4g
    "azure": re.compile(r"^(standard_)?[a-z]+\d+[a-z]*p"),  # D2ps_v5
    "gcp": re.compile(r"^(t2a|c4a)-"),
    "oci": re.compile(r"\.a\d\."),  # VM.Standard.A1.Flex
}

# --- instance names -> shape -------------------------------------------------

_AWS_RUNNING = re.compile(r"(Box|Spot|Dedicated)Usage:")
_AWS_TYPE = re.compile(
    r"\b([a-z]+\d[a-z0-9-]*)\.(nano|micro|small|medium|large|\d*xlarge|metal[\w-]*)\b"
)
_AWS_T_SIZES = {
    "nano": (2, 0.5),
    "micro": (2, 1),
    "small": (2, 2),
    "medium": (2, 4),
    "large": (2, 8),
    "xlarge": (4, 16),
    "2xlarge": (8, 32),
}
_AWS_GIB_PER_VCPU = {"a": 2, "c": 2, "m": 4, "h": 4, "r": 8, "z": 8, "i": 8, "d": 8, "x": 16}
_AWS_ACCELERATED = frozenset({"p", "g", "gr", "inf", "trn", "dl", "f", "vt"})

_AZURE_TYPE = re.compile(
    r"(?<![A-Za-z0-9])(?:Standard_)?([A-Z]{1,2})(\d+)(?:-\d+)?([a-z]*)(?:[ _](v\d+))?(?![A-Za-z0-9])"
)
_AZURE_GIB_PER_VCPU = {"A": 2, "D": 4, "E": 8, "F": 2, "L": 8}

_GCP_TYPE = re.compile(r"\b([a-z]\d[a-z]?)-(standard|highmem|highcpu)-(\d+)\b")
_GCP_CUSTOM = re.compile(r"\b(?:[a-z]\d[a-z]?-)?custom-(\d+)-(\d+)\b")
_GCP_SHARED = {
    "e2-micro": (2, 1),
    "e2-small": (2, 2),
    "e2-medium": (2, 4),
    "f1-micro": (1, 0.6),
    "g1-small": (1, 1.7),
}
_GCP_GIB_PER_VCPU = {"standard": 4, "highmem": 8, "highcpu": 1}
_GCP_N1_GIB_PER_VCPU = {"standard": 3.75, "highmem": 6.5, "highcpu": 0.9}


@dataclass(frozen=True)
class _Shape:
    vcpus: float
    gib: float
    source: str


def _find_type(cloud: str, text: str) -> str | None:
    if cloud == "aws":
        m = _AWS_TYPE.search(text)
        return f"{m.group(1)}.{m.group(2)}" if m else None
    if cloud == "azure":
        m = _AZURE_TYPE.search(text)
        if not m:
            return None
        family, size, suffix, version = m.groups()
        return f"{family}{size}{suffix}" + (f"_{version}" if version else "")
    if cloud == "gcp":
        m = _GCP_TYPE.search(text) or _GCP_CUSTOM.search(text)
        if m:
            return m.group(0)
        return next((name for name in _GCP_SHARED if name in text), None)
    return None  # OCI bills OCPU and memory, which the vCPU/memory path reads


def _accelerated(cloud: str, name: str) -> bool:
    if cloud == "aws":
        return re.match(r"[a-z]+", name).group() in _AWS_ACCELERATED  # type: ignore[union-attr]
    if cloud == "azure":
        return name.upper().startswith("N")
    return "gpu" in name.lower()


def _shape_from_name(cloud: str, name: str) -> tuple[float, float] | None:
    if cloud == "aws":
        family, size = name.split(".", 1)
        cls = re.match(r"[a-z]+", family).group()  # type: ignore[union-attr]
        if cls == "t":
            shape = _AWS_T_SIZES.get(size)
            if shape and family == "t2" and size in ("nano", "micro", "small"):
                return 1, shape[1]
            return shape
        ratio = _AWS_GIB_PER_VCPU.get(cls)
        if ratio is None:
            return None
        if size == "medium":
            vcpus = 1
        elif size == "large":
            vcpus = 2
        elif size.endswith("xlarge"):
            vcpus = 4 * int(size[:-6] or 1)
        else:
            return None  # nano/micro/small exist only for t; metal varies
        return vcpus, vcpus * ratio
    if cloud == "azure":
        m = re.match(r"([A-Z]{1,2})(\d+)([a-z]*)", name)
        if not m:
            return None
        family, vcpus, suffix = m.group(1), int(m.group(2)), m.group(3)
        if family == "B":
            return vcpus, vcpus * (4 if "m" in suffix else 2)
        ratio = _AZURE_GIB_PER_VCPU.get(family)
        return (vcpus, vcpus * ratio) if ratio else None
    if cloud == "gcp":
        if name in _GCP_SHARED:
            return _GCP_SHARED[name]
        if m := _GCP_CUSTOM.fullmatch(name):
            return int(m.group(1)), int(m.group(2)) / 1024
        if m := _GCP_TYPE.fullmatch(name):
            series, kind, vcpus = m.group(1), m.group(2), int(m.group(3))
            ratios = _GCP_N1_GIB_PER_VCPU if series == "n1" else _GCP_GIB_PER_VCPU
            return vcpus, vcpus * ratios[kind]
    return None


def _details(r: dict[str, str]) -> dict[str, Any]:
    try:
        parsed = json.loads(r.get("SkuPriceDetails") or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _instance(
    catalog: PriceCatalog, cloud: Cloud, r: dict[str, str]
) -> tuple[str, _Shape | None, str | None] | None:
    """(instance name, shape, reason it cannot be priced); None when no instance is named."""
    usage_type = r.get("x_UsageType")
    if cloud == "aws" and usage_type and not _AWS_RUNNING.search(usage_type):
        return None  # EBSOptimized:m4.large and the like are fees, not running hours
    details = _details(r)
    if named := details.get("InstanceType"):
        name = _find_type(cloud, str(named)) or str(named)
    else:
        name = next((t for c in _TEXT_COLUMNS if (t := _find_type(cloud, r.get(c) or ""))), None)
    gpus = _num(str(details.get("GpuCount", "")))
    if (gpus or 0) > 0 or (name and _accelerated(cloud, name)):
        return name or "GPU instance", None, "accelerator"
    cores, memory = (
        _num(str(details.get("CoreCount", ""))),
        _num(str(details.get("MemorySize", ""))),
    )
    if cores and memory:
        return (
            name or f"{cores:g} vCPU, {memory:g} GiB",
            _Shape(cores, memory, "sku_price_details"),
            None,
        )
    if not name:
        return None
    if known := catalog.find(cloud, name):
        return name, _Shape(known.vcpus, known.memory_gb, "catalogue"), None
    if shape := _shape_from_name(cloud, name):
        return name, _Shape(*shape, "instance_name"), None
    return name, None, "unrecognised"


# --- usage quantities --------------------------------------------------------


def _unit_kind(unit: str) -> tuple[str, float] | None:
    """('hours' | 'gib_hours', factor to hours) for a time-based unit; None otherwise."""
    if re.search(r"second|\bsec\b|\bs\b", unit):
        per = 1 / 3600
    elif re.search(r"minute|\bmin\b", unit):
        per = 1 / 60
    elif re.search(r"hour|\bhrs?\b|\bh\b", unit):
        per = 1.0
    else:
        return None  # months, requests, GB transferred
    if re.search(r"gib|giby|gibibyte|gigabyte|\bgb\b|gb-", unit):
        return "gib_hours", per
    if "byte" in unit:
        return "gib_hours", per / 2**30
    return "hours", per


def _quantity(r: dict[str, str]) -> tuple[str, float] | None:
    for qty_col, unit_col in (
        ("ConsumedQuantity", "ConsumedUnit"),
        ("PricingQuantity", "PricingUnit"),
    ):
        qty = _num(r.get(qty_col))
        unit = (r.get(unit_col) or "").strip().lower()
        if qty is None or not unit:
            continue
        if m := re.match(r"([\d.]+)\s*", unit):  # block units such as "10 Hours"
            qty *= _num(m.group(1)) or 1.0
            unit = unit[m.end() :]
        if kind := _unit_kind(unit):
            return kind[0], qty * kind[1]
    return None


def _series(r: dict[str, str]) -> str:
    """A label for vCPU/memory rows that is the same for both halves of one VM."""
    text = r.get("ChargeDescription") or ""
    if not text or "$" in text:
        text = r.get("SkuMeter") or r.get("ServiceName") or "(blank)"
    text = re.sub(r"\b(v?cpus?|cores?|ocpus?|ram|memory|mem)\b", "", text, flags=re.I)
    return re.sub(r"(\s*-\s*)+", " - ", " ".join(text.split())).strip(" -") or "(blank)"


# --- pricing -----------------------------------------------------------------


@dataclass
class _Pool:
    """Capacity that is priced as one: an instance type, or one resource's rows."""

    cloud: Cloud
    label: str
    shape: _Shape | None = None
    vcpu_hours: float = 0.0
    gib_hours: float = 0.0
    actual: float = 0.0
    rows: int = 0
    burstable: bool = False
    arm: bool = False
    labelled_by_cpu: bool = False


@dataclass
class _Skipped:
    cost: float = 0.0
    rows: int = 0
    examples: set[str] = field(default_factory=set)


_SKIP_REASONS = {
    "provider": "provider is not AWS, Azure, GCP or OCI",
    "unused": "unused commitment: there is no usage to reprice",
    "accelerator": "GPU and accelerator instances: price these with compare_gpu_workload",
    "licensed": "licensed OS (Windows, RHEL, SUSE, SQL Server): catalogue prices are Linux",
    "unrecognised": "instance type not recognised",
    "no_usage": "no instance-hours or vCPU/memory usage on the row",
    "memory_only": "memory-only charges, such as serverless functions",
}


def _targets(
    catalog: PriceCatalog, cloud: Cloud, burstable: bool, arm: bool
) -> tuple[Instance, ...]:
    out = []
    for i in catalog.by_cloud(cloud):
        sku = i.sku.lower()
        if i.gpu_count or i.hourly_usd <= 0:  # GPUs, and free-tier shapes
            continue
        if not burstable and _BURSTABLE[cloud].search(sku):
            continue
        if not arm and _ARM[cloud].search(sku):
            continue
        out.append(i)
    return tuple(out)


def _price(
    targets: tuple[Instance, ...], vcpu_hours: float, gib_hours: float, shape: _Shape | None
) -> tuple[float, Instance, float] | None:
    """(cost, instance, instance-hours) for the target that runs this capacity cheapest.

    Sizes in one family cost about the same per vCPU, so near-ties (within 0.5%)
    go to the size closest to the workload's own shape, else the smallest.
    """
    costed = [(max(vcpu_hours / i.vcpus, gib_hours / i.memory_gb), i) for i in targets]
    if not costed:
        return None
    low = min(hours * i.hourly_usd for hours, i in costed)

    def closeness(i: Instance) -> tuple[float, ...]:
        if shape is None:
            return (i.vcpus, i.memory_gb)
        return (abs(i.vcpus - shape.vcpus) + abs(i.memory_gb - shape.gib) / 8, i.vcpus)

    near = [(hours, i) for hours, i in costed if hours * i.hourly_usd <= low * 1.005]
    hours, best = min(near, key=lambda hi: (closeness(hi[1]), hi[1].sku))
    return hours * best.hourly_usd, best, hours


# --- the tool ----------------------------------------------------------------


def reprice_bill(catalog: PriceCatalog, file_path: str, top_n: int = 10) -> dict[str, Any]:
    if not 1 <= top_n <= 50:
        raise BillError("top_n must be between 1 and 50")
    rows, warnings = _load(file_path)

    pools: dict[tuple[str, str], _Pool] = {}
    skipped: dict[str, _Skipped] = defaultdict(_Skipped)
    usage_cost = compute_cost = 0.0
    days: set[str] = set()

    def skip(reason: str, cost: float, example: str) -> None:
        s = skipped[reason]
        s.cost += cost
        s.rows += 1
        if len(s.examples) < 3 and example:
            s.examples.add(example[:80])

    for r in rows:
        cost = row_cost(r)
        if cost is None or (r.get("ChargeCategory") or "Usage") != "Usage":
            continue
        usage_cost += cost
        if r.get("ServiceCategory") != "Compute":
            continue
        compute_cost += cost
        days.add((r.get("ChargePeriodStart") or "")[:10])
        provider = r.get("ProviderName") or ""
        cloud = _CLOUD_OF.get(provider.strip().lower())
        if cloud is None:
            skip("provider", cost, provider or "(blank)")
            continue
        if usage_kind(r) == "unused_commitment":
            skip("unused", cost, r.get("CommitmentDiscountId") or "")
            continue
        text = " ".join(r.get(c) or "" for c in _TEXT_COLUMNS)
        found = _instance(catalog, cloud, r)
        if (found and found[2] == "accelerator") or _GPU.search(text):
            skip("accelerator", cost, found[0] if found else _series(r))
            continue
        if _LICENSED_OS.search(text):
            skip("licensed", cost, found[0] if found else _series(r))
            continue
        qty = _quantity(r)

        if found and found[1] and qty and qty[0] == "hours":
            name, shape, _ = found
            pool = pools.setdefault((cloud, "type:" + name), _Pool(cloud, name, shape))
            pool.vcpu_hours += qty[1] * shape.vcpus
            pool.gib_hours += qty[1] * shape.gib
            pool.burstable = bool(_BURSTABLE[cloud].search(name.lower()))
            pool.arm = bool(_ARM[cloud].search(name.lower()))
        elif qty and (qty[0] == "gib_hours" or _CPU.search(text)):
            key = r.get("ResourceId") or _series(r)
            pool = pools.setdefault((cloud, "res:" + key), _Pool(cloud, _series(r)))
            if qty[0] == "gib_hours":
                pool.gib_hours += qty[1]
            else:
                ocpu = re.search(r"\bocpus?\b", text, re.I)
                arm = bool(_ARM_TEXT.search(text))
                pool.vcpu_hours += qty[1] * (2 if ocpu and not arm else 1)
                pool.arm = pool.arm or arm
                if not pool.labelled_by_cpu:  # the vCPU row names the VM best
                    pool.label, pool.labelled_by_cpu = _series(r), True
        else:
            reason = "unrecognised" if found and not found[1] else "no_usage"
            skip(reason, cost, found[0] if found else _series(r))
            continue
        pool.actual += cost
        pool.rows += 1

    if not compute_cost and not skipped:
        raise BillError("no Compute usage rows (ServiceCategory = Compute) to reprice")

    lines: dict[tuple[str, str], dict[str, Any]] = {}
    totals = dict.fromkeys(CLOUDS, 0.0)
    current_at_list = repriced = 0.0
    cache: dict[tuple[str, bool, bool], tuple[Instance, ...]] = {}
    assumed_memory = 0
    for pool in pools.values():
        if not pool.vcpu_hours:
            skip("memory_only", pool.actual, pool.label)
            skipped["memory_only"].rows += pool.rows - 1
            continue
        if not pool.gib_hours:
            pool.gib_hours = pool.vcpu_hours * _ASSUMED_GIB_PER_VCPU
            assumed_memory += 1
        priced = {}
        for target in CLOUDS:
            key = (target, pool.burstable, pool.arm)
            if key not in cache:
                cache[key] = _targets(catalog, target, pool.burstable, pool.arm)
            priced[target] = _price(cache[key], pool.vcpu_hours, pool.gib_hours, pool.shape)
        repriced += pool.actual
        if priced[pool.cloud]:
            current_at_list += priced[pool.cloud][0]

        line = lines.setdefault(
            (pool.cloud, pool.label),
            {
                "cloud": pool.cloud,
                "workload": pool.label,
                "shape": (
                    f"{pool.shape.vcpus:g} vCPU, {pool.shape.gib:g} GiB" if pool.shape else None
                ),
                "shape_source": pool.shape.source if pool.shape else "vcpu_and_memory_rows",
                "vcpu_hours": 0.0,
                "gib_hours": 0.0,
                "actual_cost": 0.0,
                "list_price": {
                    t: {"cost": 0.0, "instance_hours": 0.0, "as": set()} for t in CLOUDS
                },
            },
        )
        line["vcpu_hours"] += pool.vcpu_hours
        line["gib_hours"] += pool.gib_hours
        line["actual_cost"] += pool.actual
        for target, p in priced.items():
            if p:
                totals[target] += p[0]
                entry = line["list_price"][target]
                entry["cost"] += p[0]
                entry["instance_hours"] += p[2]
                entry["as"].add(p[1].sku)

    if assumed_memory:
        warnings.append(
            f"{assumed_memory} resource(s) had vCPU rows but no memory rows; "
            f"{_ASSUMED_GIB_PER_VCPU:g} GiB per vCPU was assumed"
        )
    if not lines:
        warnings.append("no compute on this bill could be repriced; see not_repriced")

    top = sorted(lines.values(), key=lambda line: line["actual_cost"], reverse=True)
    for line in top:
        for k in ("vcpu_hours", "gib_hours", "actual_cost"):
            line[k] = round(line[k], 2)
        prices = line["list_price"]
        for entry in prices.values():
            entry["cost"] = round(entry["cost"], 2)
            entry["instance_hours"] = round(entry["instance_hours"], 1)
            entry["as"] = ", ".join(sorted(entry["as"]))
        line["cheapest"] = min(CLOUDS, key=lambda t: prices[t]["cost"])

    sources = sorted({pool.cloud for pool in pools.values() if pool.vcpu_hours})
    by_cloud = sorted(CLOUDS, key=lambda t: totals[t])
    result = {
        "kind": "bill_repricing",
        "file": Path(file_path).name,
        "prices_as_of": catalog.as_of,
        "period": {"from": min(days - {""}, default=None), "to": max(days - {""}, default=None)},
        "source_clouds": sources,
        "coverage": {
            "usage_cost": round(usage_cost, 2),
            "compute_cost": round(compute_cost, 2),
            "repriced_cost": round(repriced, 2),
            "repriced_pct_of_compute": _pct(repriced, compute_cost),
            "repriced_pct_of_usage": _pct(repriced, usage_cost),
        },
        "totals": {
            "actual_cost": round(repriced, 2),
            "current_clouds_at_list": round(current_at_list, 2),
            "list_price_by_cloud": {t: round(totals[t], 2) for t in by_cloud},
            "cheapest": by_cloud[0] if lines else None,
        },
        "lines": top[:top_n],
        "not_repriced": [
            {
                "reason": _SKIP_REASONS[reason],
                "cost": round(s.cost, 2),
                "rows": s.rows,
                "examples": sorted(s.examples),
            }
            for reason, s in sorted(skipped.items(), key=lambda kv: -abs(kv[1].cost))
        ],
        "warnings": warnings,
        "honest_gaps": [
            "Compute only (ServiceCategory = Compute). Storage, databases, networking "
            "and managed services are not repriced yet.",
            "List price in one region per cloud (the catalogue's), Linux, on-demand. "
            "Your actual cost includes your discounts, region and any spot use; the "
            "same discounts are not guaranteed elsewhere.",
            "Capacity is priced as the vCPU and memory it used, on whichever shape "
            "runs it cheapest, without rounding up to whole instances. Shape families, "
            "CPU generations and performance differ between clouds.",
            "Shapes read from an instance name not in the catalogue are estimated "
            "from the family (shape_source = instance_name).",
            "The catalogue lists a few families per cloud, so a workload is priced "
            "on the nearest ones it has (Graviton on x86 when no ARM shape is "
            "listed), and flexible shapes (OCI Flex, GCP custom) at fixed sizes, "
            "which overstates memory-heavy work there.",
            "Moving also costs engineering time and data egress: use assess_migration "
            "for the one-time exit cost and payback.",
        ],
    }
    result["headline"] = _headline(result)
    return result


def _pct(part: float, whole: float) -> float | None:
    return round(part / whole * 100, 1) if whole else None


_NAMES = {"aws": "AWS", "azure": "Azure", "gcp": "GCP", "oci": "OCI"}


def _headline(r: dict[str, Any]) -> str:
    t, cov = r["totals"], r["coverage"]
    if not t["cheapest"]:
        return "Nothing on this bill could be repriced; see not_repriced for why."
    period = r["period"]
    line = (
        f"Repriced {t['actual_cost']:,.2f} of compute ({cov['repriced_pct_of_compute']}% of "
        f"compute, {period['from']} to {period['to']}). Same capacity at list price: "
        + ", ".join(f"{_NAMES[c]} {v:,.2f}" for c, v in t["list_price_by_cloud"].items())
        + "."
    )
    sources = r["source_clouds"]
    if len(sources) != 1:
        return line + f" Cheapest: {_NAMES[t['cheapest']]}."
    src, at_list = sources[0], t["current_clouds_at_list"]
    cheapest, low = t["cheapest"], t["list_price_by_cloud"][t["cheapest"]]
    if cheapest == src or not at_list:
        line += f" {_NAMES[src]}, where it runs today, is already the cheapest at list price."
    else:
        line += (
            f" {_NAMES[cheapest]} is cheapest, {(1 - low / at_list) * 100:.0f}% below "
            f"{_NAMES[src]} list."
        )
    if at_list and t["actual_cost"] < at_list * 0.95:
        line += (
            f" You already pay {(1 - t['actual_cost'] / at_list) * 100:.0f}% under "
            f"{_NAMES[src]} list, so the real saving depends on similar discounts there."
        )
    return line
