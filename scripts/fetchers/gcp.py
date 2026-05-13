"""GCP Cloud Billing Catalog API fetcher.

The Cloud Billing Catalog exposes every public SKU GCP charges for. The
"Compute Engine" service ID is fixed: 6F81-5844-456A.

GCP compute pricing is computed from family-level core + RAM rates rather than
looked up directly per instance type, similar to OCI:

    predefined VM (n2/c2/e2-standard/n2-highmem/n2-highcpu):
        hourly_usd = vcpus * core_rate + memory_gb * ram_rate

    shared-core VMs (e2-micro / e2-small / e2-medium):
        per-SKU fixed price published as a separate billing line each

We hit the API with an API key (free to provision in any GCP project under
"APIs & Services -> Credentials -> Create API Key", restricted to "Cloud
Billing API"). Without GCP_API_KEY the fetcher cleanly skips so the
orchestrator can still produce a snapshot of the other 3 clouds.
"""
from __future__ import annotations

import os
import re

import httpx

from scripts.fetchers.base import FetchError, InstanceSku, MissingPriceError

cloud_name = "gcp"
region = "us-east1"
_SERVICE_ID = "6F81-5844-456A"  # GCP Compute Engine
_API_BASE = f"https://cloudbilling.googleapis.com/v1/services/{_SERVICE_ID}/skus"

# Region code GCP uses inside SKU descriptions. The Billing API tags each SKU
# with a list of serviceRegions; us-east1 -> "us-east1".
_REGION_KEY = "us-east1"

# Family-level core + RAM SKU description substrings. Verified against the
# Cloud Billing Catalog response 2026-05-12 ("Americas" is the location bucket
# GCP uses for us-* regions in SKU descriptions).
_FAMILY_DESCRIPTION_PATTERNS: dict[str, tuple[str, str]] = {
    "n2": ("N2 Instance Core running in Americas", "N2 Instance Ram running in Americas"),
    "c2": ("Compute optimized Core running in Americas", "Compute optimized Ram running in Americas"),
    "e2": ("E2 Instance Core running in Americas", "E2 Instance Ram running in Americas"),
}

# GCP "Spot Preemptible" SKUs use the same family naming with "Spot Preemptible"
# prepended. v0.8.1 spot pricing fetcher.
_FAMILY_SPOT_DESCRIPTION_PATTERNS: dict[str, tuple[str, str]] = {
    "n2": ("Spot Preemptible N2 Instance Core running in Americas", "Spot Preemptible N2 Instance Ram running in Americas"),
    "c2": ("Spot Preemptible Compute optimized Core running in Americas", "Spot Preemptible Compute optimized Ram running in Americas"),
    "e2": ("Spot Preemptible E2 Instance Core running in Americas", "Spot Preemptible E2 Instance Ram running in Americas"),
}

# Shared-core SKUs price the whole VM as a single billing line.
_SHARED_CORE_SKUS = {
    "e2-micro": "Micro Instance with burstable CPU running in Americas",
    "e2-small": "Small Instance with 1 VCPU running in Americas",
    "e2-medium": "Medium Instance with 1 VCPU running in Americas",
}

_FAMILY_RE = re.compile(r"^(n2|c2|e2)-")


def fetch_instance_prices(skus: list[InstanceSku]) -> list[InstanceSku]:
    api_key = os.environ.get("GCP_API_KEY")
    if not api_key:
        raise FetchError(
            "GCP refresh skipped: GCP_API_KEY env var not set. Create a "
            "Cloud Billing API key in GCP Console (APIs & Services -> "
            "Credentials -> Create API Key, restricted to Cloud Billing API) "
            "and add it as a GitHub repo secret named GCP_API_KEY."
        )

    items = _fetch_all_skus(api_key)
    family_rates = _extract_family_rates(items)
    family_spot_rates = _extract_family_rates(items, spot=True, optional=True)
    shared_core_prices = _extract_shared_core_prices(items)

    refreshed: list[InstanceSku] = []
    for entry in skus:
        sku = entry["sku"]
        entry_out = dict(entry)

        # Shared-core SKUs ship a fixed price each. No spot variant (these are
        # already heavily discounted burstable shapes).
        if sku in _SHARED_CORE_SKUS:
            if sku not in shared_core_prices:
                raise MissingPriceError(f"GCP: shared-core SKU {sku!r} not found in API")
            entry_out["hourly_usd"] = shared_core_prices[sku]
            refreshed.append(entry_out)  # type: ignore[arg-type]
            continue

        # Predefined VMs: vcpus * core_rate + memory * ram_rate
        m = _FAMILY_RE.match(sku)
        if not m:
            raise MissingPriceError(f"GCP: cannot derive family from SKU {sku!r}")
        family = m.group(1)
        if family not in family_rates:
            raise MissingPriceError(f"GCP: family {family!r} rates not in API response")

        core_rate, ram_rate = family_rates[family]
        vcpus = float(entry["vcpus"])
        memory_gb = float(entry["memory_gb"])
        entry_out["hourly_usd"] = round(vcpus * core_rate + memory_gb * ram_rate, 6)

        spot_rates = family_spot_rates.get(family)
        if spot_rates is not None:
            spot_core, spot_ram = spot_rates
            entry_out["spot_hourly_usd"] = round(vcpus * spot_core + memory_gb * spot_ram, 6)

        refreshed.append(entry_out)  # type: ignore[arg-type]

    return refreshed


def fetch_storage_prices(skus):
    # Persistent Disk pricing. Deferred — see Azure/OCI/AWS storage rationale.
    return list(skus)


def _fetch_all_skus(api_key: str) -> list[dict]:
    """Walk every page of the Compute Engine SKUs endpoint."""
    items: list[dict] = []
    page_token = None
    try:
        with httpx.Client(timeout=30.0) as client:
            while True:
                params = {"key": api_key, "pageSize": 500}
                if page_token:
                    params["pageToken"] = page_token
                resp = client.get(_API_BASE, params=params)
                resp.raise_for_status()
                payload = resp.json()
                items.extend(payload.get("skus") or [])
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
    except httpx.HTTPError as e:
        raise FetchError(f"GCP Cloud Billing Catalog API error: {e}") from e
    return items


def _extract_family_rates(
    items: list[dict],
    *,
    spot: bool = False,
    optional: bool = False,
) -> dict[str, tuple[float, float]]:
    """Walk the SKU list and find core/RAM rates per family.

    `spot=False` returns on-demand rates and excludes spot/custom/sole-tenant/
    commitment variants. `spot=True` returns the Spot Preemptible variants
    instead.

    `optional=True` returns rates only for families where both core+RAM were
    found (used for spot lookups, where coverage is best-effort). With
    `optional=False` (default), any incomplete family raises MissingPriceError.
    """
    patterns = _FAMILY_SPOT_DESCRIPTION_PATTERNS if spot else _FAMILY_DESCRIPTION_PATTERNS
    rates: dict[str, tuple[float | None, float | None]] = {f: (None, None) for f in patterns}

    for sku in items:
        if not _is_relevant_sku(sku, spot=spot):
            continue
        description = sku.get("description") or ""
        for family, (core_pat, ram_pat) in patterns.items():
            core_rate, ram_rate = rates[family]
            if core_rate is None and core_pat in description:
                rates[family] = (_unit_price_usd(sku), ram_rate)
            elif ram_rate is None and ram_pat in description:
                rates[family] = (core_rate, _unit_price_usd(sku))

    return _finalize_family_rates(rates, optional=optional)


def _is_relevant_sku(sku: dict, *, spot: bool) -> bool:
    """Region filter + exclude variants we don't want at this lookup level."""
    if _REGION_KEY not in (sku.get("serviceRegions") or []):
        return False
    lowered = (sku.get("description") or "").lower()
    if "custom" in lowered or "sole tenant" in lowered or "commitment" in lowered:
        return False
    if spot:
        # Spot lookup: require both 'spot' and 'preemptible' in description
        # (GCP labels them "Spot Preemptible X..."). Don't match plain
        # "Preemptible" without "Spot" (legacy product, separate price).
        return ("spot" in lowered) and ("preemptible" in lowered)
    # On-demand lookup: exclude any spot/preemptible variant.
    return "preemptible" not in lowered and "spot" not in lowered


def _finalize_family_rates(
    rates: dict[str, tuple[float | None, float | None]],
    *,
    optional: bool,
) -> dict[str, tuple[float, float]]:
    completed: dict[str, tuple[float, float]] = {}
    for family, (core, ram) in rates.items():
        if core is None or ram is None:
            if optional:
                continue
            raise MissingPriceError(
                f"GCP: family {family!r} rates incomplete "
                f"(core={core}, ram={ram}). API response may have changed."
            )
        completed[family] = (core, ram)
    return completed


def _extract_shared_core_prices(items: list[dict]) -> dict[str, float]:
    """Find fixed prices for e2-micro / e2-small / e2-medium."""
    prices: dict[str, float] = {}
    for sku in items:
        description = sku.get("description") or ""
        lowered = description.lower()
        if any(skip in lowered for skip in ("preemptible", "spot")):
            continue
        if _REGION_KEY not in (sku.get("serviceRegions") or []):
            continue
        for sku_name, needle in _SHARED_CORE_SKUS.items():
            if sku_name in prices:
                continue
            if needle in description:
                prices[sku_name] = _unit_price_usd(sku)
    return prices


def _unit_price_usd(sku: dict) -> float:
    """Pull the per-hour USD rate out of a SKU's pricing tier list.

    GCP pricing is structured as:
        pricingInfo[0].pricingExpression.tieredRates[*].unitPrice
        (.units + .nanos / 1e9)

    We take the cheapest non-zero tier (GCP sometimes ships a $0 introductory
    tier for the first N GiB-hours).
    """
    pricing_info = sku.get("pricingInfo") or []
    if not pricing_info:
        return 0.0
    pe = pricing_info[0].get("pricingExpression") or {}
    tiered = pe.get("tieredRates") or []
    best: float | None = None
    for tier in tiered:
        unit_price = tier.get("unitPrice") or {}
        units = int(unit_price.get("units") or 0)
        nanos = int(unit_price.get("nanos") or 0)
        value = units + nanos / 1_000_000_000
        if value <= 0:
            continue
        if best is None or value < best:
            best = value
    return best if best is not None else 0.0
