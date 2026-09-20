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
            # NOTE: no shared-core SKUs here, deliberately. GCP publishes none
            # for the E2 family - e2-micro/small/medium bill from the E2 core
            # and RAM rates above against a fractional vCPU share.
            #
            # This fixture used to carry three "Micro/Small/Medium Instance"
            # SKUs. Those descriptions are the LEGACY N1 shapes (f1-micro,
            # g1-small), and the fixture gave them e2 prices - so the fetcher's
            # wrong mapping looked correct here while it would have published
            # e2-small 53% high against live data. A fixture that states the
            # assumption under test cannot falsify it.
            #
            # Real us-east1 rates, confirmed against the live Cloud Billing
            # Catalog 2026-09-20:
            #   Micro Instance with burstable CPU ... = 0.0076   (f1-micro)
            #   Small Instance with 1 VCPU ...        = 0.0257   (g1-small)
            # Neither is an E2 price.
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


@pytest.mark.parametrize(
    ("sku", "expected"),
    [
        # share * 0.02181 + gb * 0.002924
        ("e2-micro", 0.25 * 0.02181 + 1 * 0.002924),   # 0.008377
        ("e2-small", 0.5 * 0.02181 + 2 * 0.002924),    # 0.016753
        ("e2-medium", 1.0 * 0.02181 + 4 * 0.002924),   # 0.033506
    ],
)
def test_gcp_prices_shared_core_from_fractional_vcpu_share(httpx_mock, sku, expected):
    """Shared-core E2 shapes bill a FRACTION of a vCPU, not the advertised count.

    `vcpus` is 2 for all three shapes because that is what the guest sees.
    Billing at 2 vCPU would put e2-micro at 0.046547 - 5x its true price.
    These expectations match the live catalog to five decimal places.
    """
    httpx_mock.add_response(json=_gcp_full_response())
    result = gcp.fetch_instance_prices([
        {"sku": sku, "vcpus": 2, "memory_gb": 4},
    ])
    assert result[0]["hourly_usd"] == pytest.approx(expected, abs=1e-5)


def test_gcp_shared_core_ignores_advertised_vcpus(httpx_mock):
    """Guard the 5x overcharge directly: the `vcpus` field must not be used."""
    httpx_mock.add_response(json=_gcp_full_response())
    result = gcp.fetch_instance_prices([
        {"sku": "e2-micro", "vcpus": 2, "memory_gb": 1},
    ])
    naive = 2 * 0.02181 + 1 * 0.002924  # 0.046544
    assert result[0]["hourly_usd"] < naive / 4


def test_gcp_carries_forward_gpu_shapes_instead_of_failing(httpx_mock):
    """A shape this module cannot price must not take the whole cloud down.

    v0.11.0 added GPU SKUs to the catalog. The fetcher raised on the first one,
    and because MissingPriceError is fatal per-cloud, every GCP refresh became
    a skip - fifteen refreshable shapes stopped updating because of six this
    module never claimed to handle.
    """
    httpx_mock.add_response(json=_gcp_full_response())
    result = gcp.fetch_instance_prices([
        {"sku": "e2-standard-2", "vcpus": 2, "memory_gb": 8},
        {"sku": "n1-standard-4+t4", "vcpus": 4, "memory_gb": 15, "hourly_usd": 0.51},
        {"sku": "a2-highgpu-1g", "vcpus": 12, "memory_gb": 85, "hourly_usd": 3.673},
    ])
    by_sku = {r["sku"]: r for r in result}
    assert by_sku["e2-standard-2"]["hourly_usd"] == pytest.approx(0.067012, abs=1e-5)
    # Carried forward untouched, not recomputed and not dropped.
    assert by_sku["n1-standard-4+t4"]["hourly_usd"] == 0.51
    assert by_sku["a2-highgpu-1g"]["hourly_usd"] == 3.673


def test_gcp_still_raises_on_a_genuinely_missing_family(httpx_mock):
    """Pass-through is only for declared gaps. An unknown shape stays loud."""
    httpx_mock.add_response(json=_gcp_full_response())
    with pytest.raises(MissingPriceError):
        gcp.fetch_instance_prices([
            {"sku": "m3-ultramem-32", "vcpus": 32, "memory_gb": 976},
        ])


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
