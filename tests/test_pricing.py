from cloudprice_mcp.pricing import HOURS_PER_MONTH, load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


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


def test_storage_skus_loaded_for_every_cloud():
    catalog = load_catalog()
    for cloud in ("aws", "azure", "gcp"):
        ssd = catalog.storage_for(cloud, "ssd")
        hdd = catalog.storage_for(cloud, "hdd")
        assert ssd is not None and ssd.disk_type == "ssd"
        assert hdd is not None and hdd.disk_type == "hdd"
        assert ssd.price_per_gb_month_usd > 0
        assert hdd.price_per_gb_month_usd > 0


def test_storage_monthly_cost_scales_with_capacity_and_quantity():
    catalog = load_catalog()
    aws_ssd = catalog.storage_for("aws", "ssd")
    assert aws_ssd is not None
    expected = round(aws_ssd.price_per_gb_month_usd * 100 * 3, 2)
    assert aws_ssd.monthly_cost(capacity_gb=100, quantity=3) == expected
