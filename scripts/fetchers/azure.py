"""Azure Retail Prices API fetcher.

Unauthenticated public REST API:
    https://prices.azure.com/api/retail/prices

No API key required. OData-style $filter. Pages of up to 1000 items each.
We query per-SKU rather than fetching everything in the region, which keeps the
filter precise and the response small.

Linux on-demand pricing only. Spot, low-priority, reserved, and Windows variants
are filtered out client-side because Azure's `meterName` distinguishes them
inconsistently (sometimes via meterName, sometimes via productName).
"""
from __future__ import annotations

import httpx

from scripts.fetchers.base import FetchError, InstanceSku, MissingPriceError

cloud_name = "azure"
region = "eastus"
_API_BASE = "https://prices.azure.com/api/retail/prices"
_API_VERSION = "2023-01-01-preview"


def fetch_instance_prices(skus: list[InstanceSku]) -> list[InstanceSku]:
    refreshed: list[InstanceSku] = []
    with httpx.Client(timeout=20.0) as client:
        for entry in skus:
            sku = entry["sku"]
            arm_name = sku if sku.startswith("Standard_") else f"Standard_{sku}"
            entry_out = dict(entry)
            entry_out["hourly_usd"] = _lookup_one(client, arm_name)
            refreshed.append(entry_out)  # type: ignore[arg-type]
    return refreshed


def fetch_storage_prices(skus):
    # Azure storage has many tiers per type (P10, P15, P20...). The current
    # catalog uses single representative entries ("Standard SSD") rather than
    # listing every tier. Refreshing storage requires matching specific tier
    # SKUs not present in the input. Deferred to v0.7.1+; for now return input
    # unchanged so the orchestrator can still produce a snapshot.
    return list(skus)


def _lookup_one(client: httpx.Client, arm_sku_name: str) -> float:
    filter_q = (
        f"serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{region}' "
        f"and priceType eq 'Consumption' "
        f"and armSkuName eq '{arm_sku_name}'"
    )
    try:
        resp = client.get(
            _API_BASE,
            params={"$filter": filter_q, "api-version": _API_VERSION},
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise FetchError(f"Azure Retail Prices API error for {arm_sku_name}: {e}") from e

    items = resp.json().get("Items", [])
    linux_ondemand = [
        i for i in items
        if "Spot" not in (i.get("meterName") or "")
        and "Low Priority" not in (i.get("meterName") or "")
        and "Windows" not in (i.get("productName") or "")
    ]
    if not linux_ondemand:
        raise MissingPriceError(
            f"Azure: no Linux on-demand price found for {arm_sku_name} "
            f"(API returned {len(items)} rows). SKU may have been retired."
        )
    cheapest = min(linux_ondemand, key=lambda i: i["unitPrice"])
    return float(cheapest["unitPrice"])
