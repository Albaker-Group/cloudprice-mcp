"""Unit tests for v0.7 price-refresh fetchers.

Each fetcher hits a public pricing API in production; in tests we mock the HTTP
layer (httpx) with pytest-httpx so the suite stays fast + hermetic. AWS uses
boto3 which we don't have mocked at the network level — instead we substitute
the boto3 client with a fake.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts.fetchers import aws, azure, oci
from scripts.fetchers.base import FetchError, MissingPriceError


# --- Azure fetcher ---


def _azure_response(unit_price: float, *, meter_name: str = "D2s v5", product_name: str = "Virtual Machines DSv5 Series") -> dict:
    return {
        "Items": [
            {
                "armSkuName": "Standard_D2s_v5",
                "armRegionName": "eastus",
                "meterName": meter_name,
                "productName": product_name,
                "priceType": "Consumption",
                "unitPrice": unit_price,
            }
        ]
    }


def test_azure_refreshes_known_sku(httpx_mock):
    httpx_mock.add_response(json=_azure_response(0.0961))
    result = azure.fetch_instance_prices([
        {"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}
    ])
    assert result == [{"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8, "hourly_usd": 0.0961}]


def test_azure_skips_spot_meters(httpx_mock):
    httpx_mock.add_response(json={
        "Items": [
            {
                "armSkuName": "Standard_D2s_v5",
                "meterName": "D2s v5 Spot",
                "productName": "Virtual Machines DSv5 Series",
                "unitPrice": 0.01,  # spot — should be ignored
            },
            {
                "armSkuName": "Standard_D2s_v5",
                "meterName": "D2s v5",
                "productName": "Virtual Machines DSv5 Series",
                "unitPrice": 0.0961,  # on-demand — should win
            },
        ]
    })
    result = azure.fetch_instance_prices([{"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}])
    assert result[0]["hourly_usd"] == 0.0961


def test_azure_skips_windows_variants(httpx_mock):
    httpx_mock.add_response(json={
        "Items": [
            {
                "armSkuName": "Standard_D2s_v5",
                "meterName": "D2s v5",
                "productName": "Virtual Machines DSv5 Series Windows",  # Win SKU
                "unitPrice": 0.30,
            },
            {
                "armSkuName": "Standard_D2s_v5",
                "meterName": "D2s v5",
                "productName": "Virtual Machines DSv5 Series",  # Linux
                "unitPrice": 0.0961,
            },
        ]
    })
    result = azure.fetch_instance_prices([{"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}])
    assert result[0]["hourly_usd"] == 0.0961


def test_azure_raises_when_sku_missing(httpx_mock):
    httpx_mock.add_response(json={"Items": []})
    with pytest.raises(MissingPriceError):
        azure.fetch_instance_prices([{"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}])


def test_azure_raises_when_http_error(httpx_mock):
    httpx_mock.add_response(status_code=500)
    with pytest.raises(FetchError):
        azure.fetch_instance_prices([{"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}])


# --- OCI fetcher ---


def _oci_response() -> dict:
    """Minimal OCI catalog with E5 + A2 + A1 family rates."""
    def item(name: str, value: float) -> dict:
        return {
            "displayName": name,
            "currencyCodeLocalizations": [{"prices": [{"value": value}]}],
        }

    return {
        "items": [
            item("Compute - Standard - E5 - OCPU", 0.03),
            item("Compute - Standard - E5 - Memory", 0.002),
            item("Compute - Standard - A2 OCPU", 0.014),
            item("Compute - Standard - A2 Memory", 0.002),
            item("Compute - Standard - A1 - OCPU", 0.0),
            item("Compute - Standard - A1 - Memory", 0.0),
        ]
    }


def test_oci_computes_e5_price_from_family_rates(httpx_mock):
    httpx_mock.add_response(json=_oci_response())
    result = oci.fetch_instance_prices([
        {"sku": "VM.Standard.E5.Flex.1OCPU", "vcpus": 2, "memory_gb": 8},
    ])
    # 1 OCPU * $0.03 + 8 GB * $0.002 = $0.046
    assert result[0]["hourly_usd"] == pytest.approx(0.046)


def test_oci_handles_always_free(httpx_mock):
    httpx_mock.add_response(json=_oci_response())
    result = oci.fetch_instance_prices([
        {"sku": "VM.Standard.A1.Flex.AlwaysFree", "vcpus": 4, "memory_gb": 24},
    ])
    assert result[0]["hourly_usd"] == 0.0


def test_oci_excludes_cloud_at_customer_variants(httpx_mock):
    # Cloud@Customer variants must not be picked up as the public-cloud price.
    httpx_mock.add_response(json={
        "items": [
            {
                "displayName": "Oracle Compute Cloud@Customer - Compute - Standard - E5 - Memory",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.0004}]}],
            },
            {
                "displayName": "Compute - Standard - E5 - OCPU",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.03}]}],
            },
            {
                "displayName": "Compute - Standard - E5 - Memory",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.002}]}],
            },
            {
                "displayName": "Compute - Standard - A2 OCPU",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.014}]}],
            },
            {
                "displayName": "Compute - Standard - A2 Memory",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.002}]}],
            },
            {
                "displayName": "Compute - Standard - A1 - OCPU",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.0}]}],
            },
            {
                "displayName": "Compute - Standard - A1 - Memory",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.0}]}],
            },
        ]
    })
    result = oci.fetch_instance_prices([
        {"sku": "VM.Standard.E5.Flex.1OCPU", "vcpus": 2, "memory_gb": 8},
    ])
    # 1 * 0.03 + 8 * 0.002 = 0.046 (NOT the Cloud@Customer memory rate)
    assert result[0]["hourly_usd"] == pytest.approx(0.046)


def test_oci_raises_for_unknown_family(httpx_mock):
    httpx_mock.add_response(json=_oci_response())
    with pytest.raises(MissingPriceError):
        oci.fetch_instance_prices([
            {"sku": "VM.Standard.Z9.Flex.1OCPU", "vcpus": 2, "memory_gb": 8},
        ])


# --- AWS fetcher ---


class _FakeBoto3Client:
    """Minimal stand-in for the boto3 pricing client. Returns a single product
    matching the requested instanceType filter with a configurable hourly USD."""

    def __init__(self, price_per_hour: float = 0.192):
        self.price = price_per_hour

    def get_products(self, ServiceCode, Filters, MaxResults):
        instance_type = next(
            (f["Value"] for f in Filters if f["Field"] == "instanceType"),
            "unknown",
        )
        product = {
            "product": {"attributes": {"instanceType": instance_type}},
            "terms": {
                "OnDemand": {
                    "TERM1": {
                        "priceDimensions": {
                            "PD1": {
                                "unit": "Hrs",
                                "pricePerUnit": {"USD": f"{self.price:.10f}"},
                            }
                        }
                    }
                }
            },
        }
        return {"PriceList": [json.dumps(product)]}


def test_aws_parses_hourly_usd_from_nested_response():
    with patch.object(aws, "boto3", create=True) as fake_boto3:
        fake_boto3.client.return_value = _FakeBoto3Client(price_per_hour=0.192)
        result = aws.fetch_instance_prices([
            {"sku": "m5.xlarge", "vcpus": 4, "memory_gb": 16}
        ])
    assert result[0]["hourly_usd"] == pytest.approx(0.192)


def test_aws_skips_zero_priced_promo_rows():
    """AWS sometimes carries a $0 free-tier row; the fetcher should skip past it."""
    class TwoRowClient:
        def get_products(self, ServiceCode, Filters, MaxResults):
            promo = {"terms": {"OnDemand": {"T": {"priceDimensions": {"P": {
                "unit": "Hrs", "pricePerUnit": {"USD": "0.0000000000"}
            }}}}}}
            real = {"terms": {"OnDemand": {"T": {"priceDimensions": {"P": {
                "unit": "Hrs", "pricePerUnit": {"USD": "0.0104000000"}
            }}}}}}
            return {"PriceList": [json.dumps(promo), json.dumps(real)]}

    with patch.object(aws, "boto3", create=True) as fake_boto3:
        fake_boto3.client.return_value = TwoRowClient()
        result = aws.fetch_instance_prices([
            {"sku": "t3.micro", "vcpus": 2, "memory_gb": 1}
        ])
    assert result[0]["hourly_usd"] == pytest.approx(0.0104)


def test_aws_raises_when_no_products_returned():
    class EmptyClient:
        def get_products(self, **kw):
            return {"PriceList": []}

    with patch.object(aws, "boto3", create=True) as fake_boto3:
        fake_boto3.client.return_value = EmptyClient()
        with pytest.raises(MissingPriceError):
            aws.fetch_instance_prices([
                {"sku": "nonexistent.xlarge", "vcpus": 4, "memory_gb": 16}
            ])
