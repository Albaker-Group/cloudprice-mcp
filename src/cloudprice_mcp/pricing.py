import json
from dataclasses import dataclass
from importlib import resources
from typing import Literal

Cloud = Literal["aws", "azure", "gcp"]
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
class PriceCatalog:
    as_of: str
    currency: str
    instances: tuple[Instance, ...]

    def by_cloud(self, cloud: Cloud) -> tuple[Instance, ...]:
        return tuple(i for i in self.instances if i.cloud == cloud)

    def find(self, cloud: Cloud, sku: str) -> Instance | None:
        sku_lower = sku.lower()
        for instance in self.instances:
            if instance.cloud == cloud and instance.sku.lower() == sku_lower:
                return instance
        return None


_catalog: PriceCatalog | None = None


def load_catalog() -> PriceCatalog:
    global _catalog
    if _catalog is not None:
        return _catalog

    data_text = resources.files("cloudprice_mcp.data").joinpath("prices.json").read_text()
    raw = json.loads(data_text)

    instances: list[Instance] = []
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

    _catalog = PriceCatalog(
        as_of=raw["as_of"],
        currency=raw["currency"],
        instances=tuple(instances),
    )
    return _catalog
