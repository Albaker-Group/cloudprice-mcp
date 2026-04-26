from cloudprice_mcp.pricing import HOURS_PER_MONTH, load_catalog


def test_catalog_loads_all_three_clouds():
    catalog = load_catalog()
    assert catalog.as_of
    assert catalog.currency == "USD"
    assert len(catalog.by_cloud("aws")) > 0
    assert len(catalog.by_cloud("azure")) > 0
    assert len(catalog.by_cloud("gcp")) > 0


def test_find_existing_sku_case_insensitive():
    catalog = load_catalog()
    assert catalog.find("aws", "t3.medium") is not None
    assert catalog.find("aws", "T3.MEDIUM") is not None
    assert catalog.find("azure", "d4s_v5") is not None
    assert catalog.find("gcp", "e2-standard-4") is not None


def test_find_missing_sku_returns_none():
    catalog = load_catalog()
    assert catalog.find("aws", "totally-fake") is None


def test_monthly_is_hourly_times_730():
    catalog = load_catalog()
    instance = catalog.find("aws", "t3.medium")
    assert instance is not None
    assert instance.monthly_usd == round(instance.hourly_usd * HOURS_PER_MONTH, 2)


def test_catalog_is_cached_singleton():
    a = load_catalog()
    b = load_catalog()
    assert a is b
