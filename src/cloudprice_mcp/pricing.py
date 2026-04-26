import json
from dataclasses import dataclass
from importlib import resources
from typing import Literal

Cloud = Literal["aws", "azure", "gcp"]
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


@dataclass(frozen=True)
class PriceCatalog:
    as_of: str
    currency: str
    instances: tuple[Instance, ...]
    storage: tuple[StorageSku, ...]

    def by_cloud(self, cloud: Cloud) -> tuple[Instance, ...]:
        return tuple(i for i in self.instances if i.cloud == cloud)

    def storage_by_cloud(self, cloud: Cloud) -> tuple[StorageSku, ...]:
        return tuple(s for s in self.storage if s.cloud == cloud)

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
    for cloud in ("aws", "azure", "gcp"):
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

    _catalog = PriceCatalog(
        as_of=raw["as_of"],
        currency=raw["currency"],
        instances=tuple(instances),
        storage=tuple(storage),
    )
    return _catalog


def reset_catalog_cache() -> None:
    """Test helper — drop the singleton so a re-load re-reads the JSON."""
    global _catalog
    _catalog = None
