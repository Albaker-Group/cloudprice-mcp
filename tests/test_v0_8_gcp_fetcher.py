"""Unit tests for the v0.8.0 GCP Cloud Billing Catalog fetcher.

The fetcher requires GCP_API_KEY env var. We set a dummy value for tests
since the actual HTTP call is mocked via pytest-httpx.
"""
from __future__ import annotations

import pytest

from scripts.fetchers import gcp
from scripts.fetchers.base import FetchError, MissingPriceError


def _gcp_sku(description: str, units: int, nanos: int, regions: list[str] | None = None) -> dict:
    """Build a single SKU entry matching the Cloud Billing Catalog API shape."""
    return {
        "description": description,
        "serviceRegions": regions if regions is not None else ["us-east1"],
        "pricingInfo": [{
            "pricingExpression": {
                "tieredRates": [
                    {"unitPrice": {"units": str(units), "nanos": nanos}},
                ],
            },
        }],
    }


def _gcp_full_response() -> dict:
    """Catalog response with one SKU per family rate we look for."""
    return {
        "skus": [
            # E2 family core + RAM
            _gcp_sku("E2 Instance Core running in Americas", 0, 21810000),  # $0.02181/h per vCPU
            _gcp_sku("E2 Instance Ram running in Americas", 0, 2924000),    # $0.002924/h per GB
            # N2 family
            _gcp_sku("N2 Instance Core running in Americas", 0, 31611000),
            _gcp_sku("N2 Instance Ram running in Americas", 0, 4237000),
            # C2 (compute-optimized) family
            _gcp_sku("Compute optimized Core running in Americas", 0, 35200000),
            _gcp_sku("Compute optimized Ram running in Americas", 0, 4708000),
            # Shared-core e2 SKUs (fixed prices)
            _gcp_sku("Micro Instance with burstable CPU running in Americas", 0, 8380000),   # e2-micro
            _gcp_sku("Small Instance with 1 VCPU running in Americas", 0, 16750000),         # e2-small
            _gcp_sku("Medium Instance with 1 VCPU running in Americas", 0, 33500000),        # e2-medium
        ]
    }


@pytest.fixture(autouse=True)
def gcp_api_key(monkeypatch):
    monkeypatch.setenv("GCP_API_KEY", "dummy-key-for-tests")


def test_gcp_skips_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("GCP_API_KEY", raising=False)
    with pytest.raises(FetchError, match="GCP_API_KEY"):
        gcp.fetch_instance_prices([{"sku": "e2-standard-2", "vcpus": 2, "memory_gb": 8}])


def test_gcp_refreshes_predefined_vm(httpx_mock):
    httpx_mock.add_response(json=_gcp_full_response())
    result = gcp.fetch_instance_prices([
        {"sku": "e2-standard-2", "vcpus": 2, "memory_gb": 8},
    ])
    # 2 * $0.02181 + 8 * $0.002924 = $0.04362 + $0.023392 = $0.067012
    assert result[0]["hourly_usd"] == pytest.approx(0.067012, abs=1e-5)


def test_gcp_refreshes_shared_core_micro(httpx_mock):
    httpx_mock.add_response(json=_gcp_full_response())
    result = gcp.fetch_instance_prices([
        {"sku": "e2-micro", "vcpus": 2, "memory_gb": 1},
    ])
    # Fixed-price SKU = $0.00838/h
    assert result[0]["hourly_usd"] == pytest.approx(0.00838)


def test_gcp_computes_n2_family(httpx_mock):
    httpx_mock.add_response(json=_gcp_full_response())
    result = gcp.fetch_instance_prices([
        {"sku": "n2-standard-4", "vcpus": 4, "memory_gb": 16},
    ])
    # 4 * $0.031611 + 16 * $0.004237 = $0.126444 + $0.067792 = $0.194236
    assert result[0]["hourly_usd"] == pytest.approx(0.194236, abs=1e-5)


def test_gcp_computes_c2_family(httpx_mock):
    httpx_mock.add_response(json=_gcp_full_response())
    result = gcp.fetch_instance_prices([
        {"sku": "c2-standard-8", "vcpus": 8, "memory_gb": 32},
    ])
    # 8 * $0.0352 + 32 * $0.004708 = $0.2816 + $0.150656 = $0.432256
    assert result[0]["hourly_usd"] == pytest.approx(0.432256, abs=1e-5)


def test_gcp_skips_spot_and_custom_skus(httpx_mock):
    """Catalog includes Spot + Custom variants; fetcher must NOT pick them up
    as the on-demand rate (which would silently halve the cost)."""
    httpx_mock.add_response(json={
        "skus": [
            # Decoy spot SKU — should be ignored
            _gcp_sku("Spot Preemptible N2 Instance Core running in Americas", 0, 9500000),
            # Decoy custom SKU
            _gcp_sku("Custom Instance Core running in Americas", 0, 33500000),
            # The real ones
            _gcp_sku("N2 Instance Core running in Americas", 0, 31611000),
            _gcp_sku("N2 Instance Ram running in Americas", 0, 4237000),
            # Need E2 + C2 to satisfy the completeness check
            _gcp_sku("E2 Instance Core running in Americas", 0, 21810000),
            _gcp_sku("E2 Instance Ram running in Americas", 0, 2924000),
            _gcp_sku("Compute optimized Core running in Americas", 0, 35200000),
            _gcp_sku("Compute optimized Ram running in Americas", 0, 4708000),
        ]
    })
    result = gcp.fetch_instance_prices([
        {"sku": "n2-standard-4", "vcpus": 4, "memory_gb": 16},
    ])
    # Must be the on-demand value, not the spot $0.0095/h
    assert result[0]["hourly_usd"] == pytest.approx(0.194236, abs=1e-5)


def test_gcp_filters_by_region(httpx_mock):
    """SKUs that aren't tagged for us-east1 should be ignored."""
    httpx_mock.add_response(json={
        "skus": [
            # us-west1-only N2 core SKU — should be IGNORED
            _gcp_sku("N2 Instance Core running in Americas", 0, 99999000, regions=["us-west1"]),
            # Real us-east1 SKUs
            _gcp_sku("N2 Instance Core running in Americas", 0, 31611000),
            _gcp_sku("N2 Instance Ram running in Americas", 0, 4237000),
            _gcp_sku("E2 Instance Core running in Americas", 0, 21810000),
            _gcp_sku("E2 Instance Ram running in Americas", 0, 2924000),
            _gcp_sku("Compute optimized Core running in Americas", 0, 35200000),
            _gcp_sku("Compute optimized Ram running in Americas", 0, 4708000),
        ]
    })
    result = gcp.fetch_instance_prices([
        {"sku": "n2-standard-2", "vcpus": 2, "memory_gb": 8},
    ])
    # us-east1 rate, NOT the us-west1 decoy
    assert result[0]["hourly_usd"] == pytest.approx(2 * 0.031611 + 8 * 0.004237, abs=1e-5)


def test_gcp_raises_when_family_rates_incomplete(httpx_mock):
    """If the API stops shipping one of E2/N2/C2 we must fail loudly."""
    httpx_mock.add_response(json={
        "skus": [
            _gcp_sku("N2 Instance Core running in Americas", 0, 31611000),
            _gcp_sku("N2 Instance Ram running in Americas", 0, 4237000),
            # E2 + C2 missing entirely
        ]
    })
    with pytest.raises(MissingPriceError, match="incomplete"):
        gcp.fetch_instance_prices([
            {"sku": "n2-standard-4", "vcpus": 4, "memory_gb": 16},
        ])


def test_gcp_raises_on_http_error(httpx_mock):
    httpx_mock.add_response(status_code=403)
    with pytest.raises(FetchError):
        gcp.fetch_instance_prices([
            {"sku": "e2-standard-2", "vcpus": 2, "memory_gb": 8},
        ])
