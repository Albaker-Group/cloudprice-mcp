"""AWS Pricing API fetcher.

Uses boto3's `pricing` client. The Pricing service only runs in two regions
(us-east-1, ap-south-1) regardless of which region you're pricing.
Authentication: standard AWS credential chain — for local refresh use your
own profile; for the weekly GitHub Action, OIDC into a read-only role with
`pricing:GetProducts` permission.

This is a script-only dependency: boto3 is NOT in the package's runtime deps
(would bloat the wheel by ~80 MB for users who don't refresh). The refresh
workflow installs boto3 separately. If boto3 is missing we raise a clear error.
"""
from __future__ import annotations

import json

from scripts.fetchers.base import FetchError, InstanceSku, MissingPriceError

cloud_name = "aws"
region = "us-east-1"
_PRICING_REGION = "us-east-1"  # Pricing API endpoint, NOT the priced region
_LOCATION = "US East (N. Virginia)"


def fetch_instance_prices(skus: list[InstanceSku]) -> list[InstanceSku]:
    try:
        import boto3  # noqa: I001 — optional dep at script-time only
    except ImportError as e:
        raise FetchError(
            "AWS fetcher requires boto3. Install with `pip install boto3` "
            "before running the refresh script."
        ) from e

    client = boto3.client("pricing", region_name=_PRICING_REGION)
    refreshed: list[InstanceSku] = []
    for entry in skus:
        sku = entry["sku"]
        entry_out = dict(entry)
        entry_out["hourly_usd"] = _lookup_one(client, sku)
        refreshed.append(entry_out)  # type: ignore[arg-type]
    return refreshed


def fetch_storage_prices(skus):
    # EBS pricing. Deferred to v0.7.1+ for the same reasons as Azure/OCI storage.
    return list(skus)


def _lookup_one(client, instance_type: str) -> float:
    filters = [
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "location", "Value": _LOCATION},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
    ]
    try:
        resp = client.get_products(ServiceCode="AmazonEC2", Filters=filters, MaxResults=10)
    except Exception as e:
        raise FetchError(f"AWS Pricing API error for {instance_type}: {e}") from e

    price_list = resp.get("PriceList") or []
    if not price_list:
        raise MissingPriceError(
            f"AWS: no on-demand Linux price for {instance_type} in {_LOCATION}"
        )

    for raw in price_list:
        product = json.loads(raw)
        price = _extract_hourly_usd(product)
        if price is not None:
            return price

    raise MissingPriceError(
        f"AWS: parsed {len(price_list)} products for {instance_type} but "
        f"no positive USD/hour price found"
    )


def _extract_hourly_usd(product: dict) -> float | None:
    """Walk the OnDemand → priceDimensions tree and return the first positive
    USD/hour rate. Returns None if no qualifying dimension exists.

    Some AWS records carry a $0 row (free-tier promo) before the real on-demand
    line — those are skipped, not returned.
    """
    on_demand = product.get("terms", {}).get("OnDemand", {})
    for term in on_demand.values():
        for pd in (term.get("priceDimensions") or {}).values():
            value = _hourly_usd_from_dimension(pd)
            if value is not None:
                return value
    return None


def _hourly_usd_from_dimension(pd: dict) -> float | None:
    unit = (pd.get("unit") or "").lower()
    # AWS uses "Hrs" (the abbreviation), not "Hour". Match both.
    if "hr" not in unit and "hour" not in unit:
        return None
    usd = (pd.get("pricePerUnit") or {}).get("USD")
    if usd is None:
        return None
    value = float(usd)
    return value if value > 0 else None
