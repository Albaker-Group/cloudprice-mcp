"""GCP Cloud Billing Catalog API fetcher — DEFERRED to v0.7.1.

GCP's API requires an API key (no anonymous access). The orchestrator treats a
missing GCP_API_KEY env var as "skip GCP this run" rather than fail the whole
refresh, which is how v0.7.0 ships honest 3-cloud auto-refresh while we work
out GCP's family-vs-shared-core SKU mapping in a follow-up.

To enable (v0.7.1+):
    1. GCP Console -> APIs & Services -> Credentials -> Create API Key
    2. Restrict to "Cloud Billing API"
    3. Set env var GCP_API_KEY before running refresh_prices.py
    4. Implement the family + RAM lookup logic (E2/N2/C2/N2-highmem/N2-highcpu)
"""
from __future__ import annotations

import os

from scripts.fetchers.base import FetchError, InstanceSku

cloud_name = "gcp"
region = "us-east1"


def fetch_instance_prices(skus: list[InstanceSku]) -> list[InstanceSku]:
    if not os.environ.get("GCP_API_KEY"):
        raise FetchError(
            "GCP refresh skipped: GCP_API_KEY env var not set. "
            "GCP auto-refresh ships in v0.7.1 — until then prices stay manual."
        )
    # When v0.7.1 lands this is where the Cloud Billing Catalog API lookups go.
    raise NotImplementedError("GCP fetcher coming in v0.7.1")


def fetch_storage_prices(skus):
    return list(skus)
