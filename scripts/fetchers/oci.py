"""Oracle Cloud Infrastructure public pricing API fetcher.

Unauthenticated public REST API:
    https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/

No API key required. Returns one giant JSON blob (~620 items) covering every
SKU Oracle publishes. We pull it once per refresh and look up the OCPU + Memory
rates for each family.

OCI compute pricing is computed, not looked up directly:
    hourly_usd = (ocpu_count * ocpu_rate) + (memory_gb * memory_rate)

SKU names encode the OCPU count, e.g.:
    VM.Standard.E5.Flex.4OCPU  -> family=E5, ocpu=4
    VM.Standard.A2.Flex.1OCPU  -> family=A2, ocpu=1
    VM.Standard.A1.Flex.AlwaysFree -> family=A1 (free tier, always $0)
"""
from __future__ import annotations

import re

import httpx

from scripts.fetchers.base import FetchError, InstanceSku, MissingPriceError

cloud_name = "oci"
region = "us-ashburn-1"
_API_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"

# Map our family identifier -> (OCPU displayName substring, Memory displayName substring).
# OCI's API is inconsistent about dashes and spaces around family names, so we
# match by substring rather than exact equality. The substrings are unique enough
# to disambiguate (verified manually 2026-05-12).
_FAMILY_NAME_PATTERNS: dict[str, tuple[str, str]] = {
    "E5": ("Standard - E5 - OCPU", "Standard - E5 - Memory"),
    "A2": ("Standard - A2 OCPU", "Standard - A2 Memory"),
    "A1": ("Standard - A1 - OCPU", "Standard - A1 - Memory"),
}

_SKU_FAMILY_RE = re.compile(r"VM\.Standard\.([A-Z]\d+)\.Flex")
_SKU_OCPU_RE = re.compile(r"\.(\d+)OCPU$", re.IGNORECASE)


# OCI Preemptible Instances are billed at a flat 50% discount on the equivalent
# on-demand Flex shape (per Oracle's published pricing as of 2026-05; no separate
# spot API — the discount is a regional billing rule, not a per-SKU SKU).
_OCI_PREEMPTIBLE_DISCOUNT = 0.50


def fetch_instance_prices(skus: list[InstanceSku]) -> list[InstanceSku]:
    rates = _fetch_family_rates()
    refreshed: list[InstanceSku] = []
    for entry in skus:
        sku = entry["sku"]

        # GPU SKUs (VM.GPU.*, BM.GPU.*) don't follow the OCPU+memory pricing
        # model the API exposes — their prices are bundled per-shape published
        # rates that aren't returned by the public catalog endpoint. Preserve
        # whatever's already in the bundled catalog rather than fail-the-refresh.
        # Operators update GPU prices manually when Oracle publishes changes.
        if sku.startswith(("VM.GPU.", "BM.GPU")):
            refreshed.append(dict(entry))  # type: ignore[arg-type]
            continue

        family = _family_from_sku(sku)
        if family is None:
            # Unknown shape (not Flex, not GPU). Preserve catalog value rather
            # than break the whole refresh — surface in logs via the orchestrator.
            refreshed.append(dict(entry))  # type: ignore[arg-type]
            continue

        if "AlwaysFree" in sku:
            # Always Free tier — A1.Flex up to 4 OCPU + 24 GB. Hardcoded $0.
            entry_out = dict(entry)
            entry_out["hourly_usd"] = 0.0
            entry_out["spot_hourly_usd"] = 0.0  # already free, no further discount
            refreshed.append(entry_out)  # type: ignore[arg-type]
            continue

        ocpu_count = _ocpu_from_sku(sku)
        if ocpu_count is None:
            # v0.11.1: preserve catalog rather than crash. Unusual SKU names
            # (e.g. .AlwaysFree handled above, or future naming variants)
            # shouldn't break the whole refresh.
            refreshed.append(dict(entry))  # type: ignore[arg-type]
            continue
        if family not in rates:
            # v0.11.1: family parsed but OCI's API didn't return rates for it
            # (new family Oracle just launched, etc.). Preserve catalog value.
            refreshed.append(dict(entry))  # type: ignore[arg-type]
            continue

        ocpu_rate, mem_rate = rates[family]
        memory_gb = float(entry["memory_gb"])
        hourly = round(ocpu_count * ocpu_rate + memory_gb * mem_rate, 6)
        entry_out = dict(entry)
        entry_out["hourly_usd"] = hourly
        entry_out["spot_hourly_usd"] = round(hourly * (1 - _OCI_PREEMPTIBLE_DISCOUNT), 6)
        refreshed.append(entry_out)  # type: ignore[arg-type]
    return refreshed


def fetch_storage_prices(skus):
    # Block Volume storage. Deferred to v0.7.1+ — see Azure fetcher rationale.
    return list(skus)


def _fetch_family_rates() -> dict[str, tuple[float, float]]:
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(_API_URL, params={"currencyCode": "USD"})
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise FetchError(f"OCI pricing API error: {e}") from e

    items = resp.json().get("items") or []
    rates: dict[str, tuple[float, float]] = {}
    for family, (ocpu_pat, mem_pat) in _FAMILY_NAME_PATTERNS.items():
        ocpu_rate = _find_unit_price(items, ocpu_pat)
        mem_rate = _find_unit_price(items, mem_pat)
        if ocpu_rate is None or mem_rate is None:
            raise MissingPriceError(
                f"OCI: family {family!r} rates not in API "
                f"(ocpu found={ocpu_rate is not None}, mem found={mem_rate is not None})"
            )
        rates[family] = (ocpu_rate, mem_rate)
    return rates


def _find_unit_price(items: list[dict], substring: str) -> float | None:
    """Return the first item whose displayName contains `substring`.

    OCI lists Cloud@Customer variants alongside the public-cloud SKUs. We exclude
    those (they have different prices that don't apply to standard tenancies).
    """
    for it in items:
        name = it.get("displayName") or ""
        if substring not in name:
            continue
        if "Cloud@Customer" in name:
            continue
        locs = it.get("currencyCodeLocalizations") or []
        if not locs:
            continue
        prices = locs[0].get("prices") or []
        if not prices:
            continue
        val = prices[0].get("value")
        if val is None:
            continue
        return float(val)
    return None


def _family_from_sku(sku: str) -> str | None:
    m = _SKU_FAMILY_RE.search(sku)
    return m.group(1) if m else None


def _ocpu_from_sku(sku: str) -> int | None:
    m = _SKU_OCPU_RE.search(sku)
    return int(m.group(1)) if m else None
