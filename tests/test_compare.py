from cloudprice_mcp.compare import best_match, compare_all_clouds
from cloudprice_mcp.pricing import load_catalog


def test_best_match_meets_or_exceeds_spec():
    catalog = load_catalog()
    match = best_match(catalog, "aws", vcpus=4, memory_gb=16)
    assert match is not None
    assert match.instance.vcpus >= 4
    assert match.instance.memory_gb >= 16


def test_compare_returns_one_per_cloud_sorted_by_price():
    catalog = load_catalog()
    matches = compare_all_clouds(catalog, vcpus=4, memory_gb=16)
    assert len(matches) == 3
    clouds = {m.cloud for m in matches}
    assert clouds == {"aws", "azure", "gcp"}
    prices = [m.instance.monthly_usd for m in matches]
    assert prices == sorted(prices)


def test_cheapest_picked_when_two_equally_match():
    catalog = load_catalog()
    matches = compare_all_clouds(catalog, vcpus=2, memory_gb=4)
    aws_match = next(m for m in matches if m.cloud == "aws")
    aws_candidates = [
        i for i in catalog.by_cloud("aws")
        if i.vcpus >= 2 and i.memory_gb >= 4
    ]
    cheapest_eligible = min(aws_candidates, key=lambda i: i.hourly_usd)
    assert aws_match.instance.sku == cheapest_eligible.sku
