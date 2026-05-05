import json
from dataclasses import dataclass
from importlib import resources
from typing import Literal

Cloud = Literal["aws", "azure", "gcp", "oci"]
DiskType = Literal["ssd", "hdd"]
HOURS_PER_MONTH = 730


@dataclass(frozen=True)
class Instance:
    cloud: Cloud
    sku: str
    vcpus: int
    memory_gb: float
    hourly_usd: float
    region: str

    @property
    def monthly_usd(self) -> float:
        return round(self.hourly_usd * HOURS_PER_MONTH, 2)

    def to_dict(self) -> dict:
        return {
            "cloud": self.cloud,
            "sku": self.sku,
            "region": self.region,
            "vcpus": self.vcpus,
            "memory_gb": self.memory_gb,
            "hourly_usd": self.hourly_usd,
            "monthly_usd": self.monthly_usd,
        }


@dataclass(frozen=True)
class StorageSku:
    cloud: Cloud
    sku: str
    disk_type: DiskType
    price_per_gb_month_usd: float
    snapshot_per_gb_month_usd: float
    region: str

    def monthly_cost(self, capacity_gb: float, quantity: int = 1) -> float:
        return round(self.price_per_gb_month_usd * capacity_gb * quantity, 2)

    def snapshot_monthly_cost(
        self, capacity_gb: float, quantity: int = 1, snapshot_count: int = 1
    ) -> float:
        return round(
            self.snapshot_per_gb_month_usd * capacity_gb * quantity * snapshot_count, 2
        )

    def to_dict(self) -> dict:
        return {
            "cloud": self.cloud,
            "sku": self.sku,
            "region": self.region,
            "disk_type": self.disk_type,
            "price_per_gb_month_usd": self.price_per_gb_month_usd,
            "snapshot_per_gb_month_usd": self.snapshot_per_gb_month_usd,
        }


ObjectStorageTier = Literal["hot", "cool", "archive"]


@dataclass(frozen=True)
class ObjectStorageSku:
    cloud: Cloud
    service: str
    sku: str
    tier: ObjectStorageTier
    price_per_gb_month_usd: float
    region: str
    capacity_gb_limit: float | None = None  # set on Always-Free SKUs (e.g., OCI 20 GB)

    def monthly_cost(self, capacity_gb: float, quantity: int = 1) -> float:
        return round(self.price_per_gb_month_usd * capacity_gb * quantity, 2)

    def to_dict(self) -> dict:
        out = {
            "cloud": self.cloud,
            "service": self.service,
            "sku": self.sku,
            "tier": self.tier,
            "region": self.region,
            "price_per_gb_month_usd": self.price_per_gb_month_usd,
        }
        if self.capacity_gb_limit is not None:
            out["capacity_gb_limit"] = self.capacity_gb_limit
        return out


@dataclass(frozen=True)
class PostgresSku:
    cloud: Cloud
    service: str
    sku: str
    vcpus: int
    memory_gb: float
    hourly_usd: float
    storage_per_gb_month_usd: float
    region: str

    @property
    def monthly_usd(self) -> float:
        return round(self.hourly_usd * HOURS_PER_MONTH, 2)

    def to_dict(self) -> dict:
        return {
            "cloud": self.cloud,
            "service": self.service,
            "sku": self.sku,
            "region": self.region,
            "vcpus": self.vcpus,
            "memory_gb": self.memory_gb,
            "hourly_usd": self.hourly_usd,
            "monthly_usd": self.monthly_usd,
            "storage_per_gb_month_usd": self.storage_per_gb_month_usd,
        }


@dataclass(frozen=True)
class PriceCatalog:
    as_of: str
    currency: str
    instances: tuple[Instance, ...]
    storage: tuple[StorageSku, ...]
    postgres: tuple[PostgresSku, ...] = ()
    object_storage: tuple[ObjectStorageSku, ...] = ()

    def by_cloud(self, cloud: Cloud) -> tuple[Instance, ...]:
        return tuple(i for i in self.instances if i.cloud == cloud)

    def storage_by_cloud(self, cloud: Cloud) -> tuple[StorageSku, ...]:
        return tuple(s for s in self.storage if s.cloud == cloud)

    def postgres_by_cloud(self, cloud: Cloud) -> tuple[PostgresSku, ...]:
        return tuple(p for p in self.postgres if p.cloud == cloud)

    def object_storage_by_cloud(self, cloud: Cloud) -> tuple[ObjectStorageSku, ...]:
        return tuple(o for o in self.object_storage if o.cloud == cloud)

    def find(self, cloud: Cloud, sku: str) -> Instance | None:
        sku_lower = sku.lower()
        for instance in self.instances:
            if instance.cloud == cloud and instance.sku.lower() == sku_lower:
                return instance
        return None

    def storage_for(self, cloud: Cloud, disk_type: DiskType) -> StorageSku | None:
        for s in self.storage:
            if s.cloud == cloud and s.disk_type == disk_type:
                return s
        return None


_catalog: PriceCatalog | None = None


def load_catalog() -> PriceCatalog:
    global _catalog
    if _catalog is not None:
        return _catalog

    data_text = resources.files("cloudprice_mcp.data").joinpath("prices.json").read_text()
    raw = json.loads(data_text)

    instances: list[Instance] = []
    storage: list[StorageSku] = []
    for cloud in ("aws", "azure", "gcp", "oci"):
        if cloud not in raw:
            continue  # cloud may be absent during incremental rollout
        block = raw[cloud]
        region = block["region"]
        for entry in block["instances"]:
            instances.append(
                Instance(
                    cloud=cloud,
                    sku=entry["sku"],
                    vcpus=int(entry["vcpus"]),
                    memory_gb=float(entry["memory_gb"]),
                    hourly_usd=float(entry["hourly_usd"]),
                    region=region,
                )
            )
        for entry in block.get("storage", []):
            storage.append(
                StorageSku(
                    cloud=cloud,
                    sku=entry["sku"],
                    disk_type=entry["disk_type"],
                    price_per_gb_month_usd=float(entry["price_per_gb_month_usd"]),
                    snapshot_per_gb_month_usd=float(entry.get("snapshot_per_gb_month_usd", 0.0)),
                    region=region,
                )
            )

    postgres = _load_postgres_catalog()
    object_storage = _load_object_storage_catalog()

    _catalog = PriceCatalog(
        as_of=raw["as_of"],
        currency=raw["currency"],
        instances=tuple(instances),
        storage=tuple(storage),
        postgres=tuple(postgres),
        object_storage=tuple(object_storage),
    )
    return _catalog


def _load_postgres_catalog() -> list[PostgresSku]:
    """Load managed-Postgres pricing data. Optional file — if missing, returns []."""
    try:
        data_text = (
            resources.files("cloudprice_mcp.data").joinpath("postgres_prices.json").read_text()
        )
    except FileNotFoundError:
        return []

    raw = json.loads(data_text)
    out: list[PostgresSku] = []
    for cloud in ("aws", "azure", "gcp", "oci"):
        if cloud not in raw:
            continue
        block = raw[cloud]
        service = block["service"]
        region = block["region"]
        storage_rate = float(block["storage_per_gb_month_usd"])
        for entry in block["instances"]:
            out.append(
                PostgresSku(
                    cloud=cloud,
                    service=service,
                    sku=entry["sku"],
                    vcpus=int(entry["vcpus"]),
                    memory_gb=float(entry["memory_gb"]),
                    hourly_usd=float(entry["hourly_usd"]),
                    storage_per_gb_month_usd=storage_rate,
                    region=region,
                )
            )
    return out


def _load_object_storage_catalog() -> list[ObjectStorageSku]:
    """Load object storage pricing data. Optional file — if missing, returns []."""
    try:
        data_text = (
            resources.files("cloudprice_mcp.data")
            .joinpath("object_storage_prices.json")
            .read_text()
        )
    except FileNotFoundError:
        return []

    raw = json.loads(data_text)
    out: list[ObjectStorageSku] = []
    for cloud in ("aws", "azure", "gcp", "oci"):
        if cloud not in raw:
            continue
        block = raw[cloud]
        service = block["service"]
        region = block["region"]
        for entry in block["tiers"]:
            cap_limit = entry.get("capacity_gb_limit")
            out.append(
                ObjectStorageSku(
                    cloud=cloud,
                    service=service,
                    sku=entry["sku"],
                    tier=entry["tier"],
                    price_per_gb_month_usd=float(entry["price_per_gb_month_usd"]),
                    region=region,
                    capacity_gb_limit=float(cap_limit) if cap_limit is not None else None,
                )
            )
    return out


def reset_catalog_cache() -> None:
    """Test helper — drop the singleton so a re-load re-reads the JSON."""
    global _catalog
    _catalog = None
