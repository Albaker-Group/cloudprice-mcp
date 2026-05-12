"""Per-cloud price fetchers.

Each fetcher refreshes the on-demand hourly price for a fixed list of SKUs
against the cloud provider's public pricing API.

Contract — `fetch_instance_prices(skus, region)`:
    Input: list of {"sku": str, "vcpus": int, "memory_gb": float} dicts
           (the SKU set we already track in data/prices.json; we do NOT
           auto-discover new SKUs because that would silently change
           assess_migration behavior)
    Output: same list with an added "hourly_usd" field, plus any SKU-specific
            metadata that already existed in the input dict preserved.
    Raises: FetchError on transport failures, MissingPriceError on lookup misses.

We never half-refresh. If any SKU is missing in the upstream API the fetcher
raises and the orchestrator skips writing the snapshot — better to keep the
last-known catalog than commit a partial one.
"""
from __future__ import annotations
