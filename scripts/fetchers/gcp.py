"""GCP Cloud Billing Catalog API fetcher.

STATUS (verified 2026-09-20 against the live Cloud Billing Catalog): working.

The previous status note claimed the family core/RAM SKUs had been retired and
that GCP had "consolidated predefined-VM pricing into custom-shape billing".
That was wrong. `E2/N2 Instance Core`, `... Instance Ram` and
`Compute optimized Core/Ram` are all still published for us-east1, and the
family-rate path reproduces the catalog exactly (e2-standard-2 computes
0.067011 against a catalog 0.067).

The real fault was narrower and more dangerous. GCP publishes NO shared-core
billing line for the E2 family - not for e2-micro, e2-small or e2-medium. The
three SKU descriptions this fetcher matched:

    "Micro Instance with burstable CPU running in Americas"  -> 0.0076
    "Small Instance with 1 VCPU running in Americas"         -> 0.0257

are the LEGACY N1 shapes, f1-micro and g1-small. Wrong machine family. Had
they been written to the catalog, e2-small would have been published at 0.0257
against a true 0.016753 - 53% too high - and e2-micro 9% too low.

That never happened, only by luck: e2-medium's equivalent SKU does not exist
at all, so the lookup raised MissingPriceError and the orchestrator skipped
GCP entirely before any bad value was written. The "GCP is broken" symptom was
the thing protecting the catalog. It had been skipping since 2026-05, so the
GCP half of the catalog simply stopped refreshing and nobody was told.

Shared-core E2 shapes are billed from the ordinary E2 core/RAM rates against a
FRACTIONAL vCPU share, which is why they have no SKU of their own:

    e2-micro   0.25 vCPU + 1 GB
    e2-small   0.5  vCPU + 2 GB
    e2-medium  1.0  vCPU + 4 GB

Verified 2026-09-20 - the share model reproduces all three catalog values to
five decimal places:

    e2-micro   0.25*0.02181159 + 1*0.00292353 = 0.008376  (catalog 0.00838)
    e2-small   0.5 *0.02181159 + 2*0.00292353 = 0.016753  (catalog 0.01675)
    e2-medium  1.0 *0.02181159 + 4*0.00292353 = 0.033506  (catalog 0.0335)

Note the share is NOT the advertised vCPU count - `vcpus` in the catalog is 2
for all three shapes, since that is what the shape exposes to the guest. Using
it would overcharge e2-micro by 5x.

A side benefit: shared-core shapes now get spot prices too. The old fixed-SKU
path could not produce them.
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

# Shared-core E2 shapes have no billing line of their own. They bill from the
# ordinary E2 core/RAM rates against a fractional vCPU share.
#
# These are (vcpu_share, memory_gb) - deliberately NOT the catalog's `vcpus`
# field, which reports what the guest sees (2 for all three shapes). Billing
# them at 2 vCPU would overcharge e2-micro by 5x. See the module docstring.
_SHARED_CORE_SHARES: dict[str, tuple[float, float]] = {
    "e2-micro": (0.25, 1.0),
    "e2-small": (0.5, 2.0),
    "e2-medium": (1.0, 4.0),
}

_FAMILY_RE = re.compile(r"^(n2|c2|e2)-")

# Shapes this fetcher cannot price yet. GPU-bearing shapes bill as a base VM
# plus one or more accelerator SKUs ("Nvidia Tesla T4 GPU running in Americas"
# and friends), which is a different lookup this module does not implement.
#
# They are passed through with their existing price rather than raising,
# because raising is fatal for the WHOLE cloud: v0.11.0 added these GPU SKUs
# to the catalog, and from that day the first one hit turned every GCP refresh
# into a skip. Fifteen perfectly refreshable shapes stopped updating because of
# five this module never claimed to handle.
#
# Passing through is not the same as pretending. fetch_instance_prices returns
# the list of skipped SKUs so the orchestrator can report them, and anything
# NOT matched here still raises - a SKU that silently vanishes upstream must
# still be loud.
_UNPRICED_RE = re.compile(r"^(n1|g2|a2|a3)-|\+")


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

    refreshed: list[InstanceSku] = []
    unpriced: list[str] = []
    for entry in skus:
        sku = entry["sku"]
        entry_out = dict(entry)

        if _UNPRICED_RE.search(sku):
            # Known gap, not a surprise - keep the existing price and move on.
            unpriced.append(sku)
            refreshed.append(entry_out)  # type: ignore[arg-type]
            continue

        m = _FAMILY_RE.match(sku)
        if not m:
            raise MissingPriceError(f"GCP: cannot derive family from SKU {sku!r}")
        family = m.group(1)
        if family not in family_rates:
            raise MissingPriceError(f"GCP: family {family!r} rates not in API response")

        core_rate, ram_rate = family_rates[family]

        # Shared-core shapes bill a FRACTION of a vCPU, so they use the share
        # table rather than the advertised `vcpus`. Everything else is
        # vcpus * core_rate + memory * ram_rate.
        if sku in _SHARED_CORE_SHARES:
            billed_cores, memory_gb = _SHARED_CORE_SHARES[sku]
        else:
            billed_cores = float(entry["vcpus"])
            memory_gb = float(entry["memory_gb"])

        entry_out["hourly_usd"] = round(billed_cores * core_rate + memory_gb * ram_rate, 6)

        spot_rates = family_spot_rates.get(family)
        if spot_rates is not None:
            spot_core, spot_ram = spot_rates
            entry_out["spot_hourly_usd"] = round(
                billed_cores * spot_core + memory_gb * spot_ram, 6
            )

        refreshed.append(entry_out)  # type: ignore[arg-type]

    if unpriced:
        print(
            f"  note: gcp carried forward {len(unpriced)} GPU shape(s) this "
            f"module cannot price yet: {', '.join(unpriced)}"
        )

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
