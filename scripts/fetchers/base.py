"""Shared types + errors for per-cloud fetchers."""
from __future__ import annotations

from typing import Protocol, TypedDict


class InstanceSku(TypedDict):
    sku: str
    vcpus: int
    memory_gb: float
    hourly_usd: float  # the field every fetcher is responsible for refreshing


class StorageSku(TypedDict, total=False):
    sku: str
    disk_type: str  # "ssd" | "hdd"
    price_per_gb_month_usd: float
    snapshot_per_gb_month_usd: float


class FetchError(RuntimeError):
    """Network / upstream-API failure. Orchestrator should NOT write a snapshot."""


class MissingPriceError(LookupError):
    """A SKU we asked for wasn't found in the upstream API.

    Almost always means the cloud renamed/retired the SKU. Caller should treat
    as fatal for the refresh — humans must investigate before committing.
    """


class CloudFetcher(Protocol):
    """Each cloud module exposes one of these. Pure function — no I/O at import."""

    cloud_name: str
    region: str

    def fetch_instance_prices(
        self, skus: list[InstanceSku]
    ) -> list[InstanceSku]: ...

    def fetch_storage_prices(
        self, skus: list[StorageSku]
    ) -> list[StorageSku]: ...
