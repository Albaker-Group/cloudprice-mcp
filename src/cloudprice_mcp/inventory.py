"""
Workload-inventory parser for v0.6 FinOps decision tools.

Reads YAML / dict input → canonical `WorkloadInventory` dataclass with strong typing
and validation. The canonical shape is consumed by every FinOps tool (`assess_migration`,
`optimize_commitment`, `compare_tco`, `find_egress_arbitrage`).

YAML is the v0.6 priority — it's the easiest to write by hand, IaC-friendly, and matches
how engineers already describe workloads. CSV / JSON parsers will be added if external
demand asks for them.

Example minimal YAML:
    source_cloud: aws
    compute:
      - { name: api, vcpus: 4, memory_gb: 16, quantity: 6 }
    egress:
      - { name: internet, gb_per_month: 5000 }

Example full YAML:
    source_cloud: aws
    commitment: 1yr_no_upfront
    multi_az: true
    one_time:
      data_to_migrate_gb: 50000
    compute:
      - { name: api, vcpus: 4, memory_gb: 16, quantity: 6, multi_az: true,
          os_disk_gb: 100, snapshot_count: 7, snapshot_incremental_factor: 0.3 }
    storage:
      - { name: app-data, capacity_gb: 2000, disk_type: ssd, snapshot_count: 7 }
    object_storage:
      - { name: media, capacity_gb: 50000, tier: hot }
    databases:
      - { name: primary, engine: postgres, vcpus: 8, memory_gb: 32, storage_gb: 500 }
    egress:
      - { name: internet, gb_per_month: 5000 }
      - { name: replica, gb_per_month: 2000, direction: inter_region }
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# --- Type aliases ---

DiskType = Literal["ssd", "hdd"]
ObjectTier = Literal["hot", "cool", "archive"]
EgressDirection = Literal["out_to_internet", "inter_region"]
Commitment = Literal["none", "1yr_no_upfront", "1yr_all_upfront", "3yr_no_upfront", "3yr_partial_upfront", "3yr_all_upfront"]
DBEngine = Literal["postgres"]  # MySQL / SQL Server / Oracle DB are deferred to later versions

VALID_DISK_TYPES = {"ssd", "hdd"}
VALID_OBJECT_TIERS = {"hot", "cool", "archive"}
VALID_EGRESS_DIRECTIONS = {"out_to_internet", "inter_region"}
VALID_COMMITMENTS = {
    "none",
    "1yr_no_upfront",
    "1yr_all_upfront",
    "3yr_no_upfront",
    "3yr_partial_upfront",
    "3yr_all_upfront",
}
VALID_DB_ENGINES = {"postgres"}


class InventoryError(ValueError):
    """Raised when an inventory document fails parsing or validation."""


# --- Dataclasses (canonical schema) ---


@dataclass
class ComputeItem:
    name: str
    vcpus: int
    memory_gb: float
    quantity: int = 1
    multi_az: bool = False
    os_disk_gb: float | None = None
    os_disk_type: str = "ssd"
    snapshot_count: int = 0
    snapshot_incremental_factor: float = 1.0


@dataclass
class StorageItem:
    name: str
    capacity_gb: float
    disk_type: str = "ssd"
    quantity: int = 1
    snapshot_count: int = 0
    snapshot_incremental_factor: float = 1.0


@dataclass
class ObjectStorageItem:
    name: str
    capacity_gb: float
    tier: str = "hot"
    quantity: int = 1


@dataclass
class DatabaseItem:
    name: str
    vcpus: int
    memory_gb: float
    storage_gb: float = 0.0
    engine: str = "postgres"
    quantity: int = 1


@dataclass
class EgressItem:
    name: str
    gb_per_month: float
    direction: str = "out_to_internet"


@dataclass
class OneTime:
    data_to_migrate_gb: float = 0.0


@dataclass
class WorkloadInventory:
    source_cloud: str | None = None
    commitment: str = "none"
    multi_az: bool = False
    one_time: OneTime = field(default_factory=OneTime)
    compute: list[ComputeItem] = field(default_factory=list)
    storage: list[StorageItem] = field(default_factory=list)
    object_storage: list[ObjectStorageItem] = field(default_factory=list)
    databases: list[DatabaseItem] = field(default_factory=list)
    egress: list[EgressItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Round-trip back to a plain dict. Useful for JSON output / debugging."""
        return asdict(self)

    def is_empty(self) -> bool:
        """True if no workload items at all — a useful sanity check before tool execution."""
        return not (
            self.compute or self.storage or self.object_storage or self.databases or self.egress
        )


# --- Parsers ---


def parse_yaml(text: str) -> WorkloadInventory:
    """Parse a YAML document string into a WorkloadInventory.

    Raises InventoryError on any parsing or validation failure with a clear message.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise InventoryError(f"Invalid YAML: {e}") from e

    if raw is None:
        raise InventoryError("Inventory YAML is empty.")
    if not isinstance(raw, dict):
        raise InventoryError(
            f"Inventory YAML must be a mapping (dict), got {type(raw).__name__}."
        )

    return parse_dict(raw)


def parse_yaml_file(path: Path | str) -> WorkloadInventory:
    """Parse a YAML file path into a WorkloadInventory."""
    p = Path(path)
    if not p.exists():
        raise InventoryError(f"Inventory file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise InventoryError(f"Could not read inventory file {p}: {e}") from e
    return parse_yaml(text)


def parse_dict(raw: dict) -> WorkloadInventory:
    """Parse a pre-loaded dict (already from YAML / JSON) into a WorkloadInventory.

    This is the validation entry point — all field-level checks happen here.
    """
    inv = WorkloadInventory()

    # Top-level fields
    if "source_cloud" in raw:
        inv.source_cloud = _validate_cloud(raw["source_cloud"], context="source_cloud")
    if "commitment" in raw:
        inv.commitment = _validate_choice(
            raw["commitment"], VALID_COMMITMENTS, context="commitment"
        )
    if "multi_az" in raw:
        inv.multi_az = _validate_bool(raw["multi_az"], context="multi_az")
    if "one_time" in raw:
        ot_raw = raw["one_time"]
        if not isinstance(ot_raw, dict):
            raise InventoryError(f"one_time must be a mapping, got {type(ot_raw).__name__}")
        inv.one_time = OneTime(
            data_to_migrate_gb=_validate_nonneg_number(
                ot_raw.get("data_to_migrate_gb", 0), context="one_time.data_to_migrate_gb"
            )
        )

    # Item lists
    inv.compute = _parse_list(raw.get("compute", []), _parse_compute, "compute")
    inv.storage = _parse_list(raw.get("storage", []), _parse_storage, "storage")
    inv.object_storage = _parse_list(
        raw.get("object_storage", []), _parse_object_storage, "object_storage"
    )
    inv.databases = _parse_list(raw.get("databases", []), _parse_database, "databases")
    inv.egress = _parse_list(raw.get("egress", []), _parse_egress, "egress")

    return inv


# --- Per-item parsers ---


def _parse_compute(item: dict, ctx: str) -> ComputeItem:
    _require_keys(item, {"name", "vcpus", "memory_gb"}, ctx)
    return ComputeItem(
        name=str(item["name"]),
        vcpus=_validate_pos_int(item["vcpus"], context=f"{ctx}.vcpus"),
        memory_gb=_validate_pos_number(item["memory_gb"], context=f"{ctx}.memory_gb"),
        quantity=_validate_pos_int(item.get("quantity", 1), context=f"{ctx}.quantity"),
        multi_az=_validate_bool(item.get("multi_az", False), context=f"{ctx}.multi_az"),
        os_disk_gb=(
            _validate_nonneg_number(item["os_disk_gb"], context=f"{ctx}.os_disk_gb")
            if item.get("os_disk_gb") is not None
            else None
        ),
        os_disk_type=_validate_choice(
            item.get("os_disk_type", "ssd"), VALID_DISK_TYPES, context=f"{ctx}.os_disk_type"
        ),
        snapshot_count=_validate_nonneg_int(
            item.get("snapshot_count", 0), context=f"{ctx}.snapshot_count"
        ),
        snapshot_incremental_factor=_validate_unit_interval(
            item.get("snapshot_incremental_factor", 1.0),
            context=f"{ctx}.snapshot_incremental_factor",
        ),
    )


def _parse_storage(item: dict, ctx: str) -> StorageItem:
    _require_keys(item, {"name", "capacity_gb"}, ctx)
    return StorageItem(
        name=str(item["name"]),
        capacity_gb=_validate_pos_number(item["capacity_gb"], context=f"{ctx}.capacity_gb"),
        disk_type=_validate_choice(
            item.get("disk_type", "ssd"), VALID_DISK_TYPES, context=f"{ctx}.disk_type"
        ),
        quantity=_validate_pos_int(item.get("quantity", 1), context=f"{ctx}.quantity"),
        snapshot_count=_validate_nonneg_int(
            item.get("snapshot_count", 0), context=f"{ctx}.snapshot_count"
        ),
        snapshot_incremental_factor=_validate_unit_interval(
            item.get("snapshot_incremental_factor", 1.0),
            context=f"{ctx}.snapshot_incremental_factor",
        ),
    )


def _parse_object_storage(item: dict, ctx: str) -> ObjectStorageItem:
    _require_keys(item, {"name", "capacity_gb"}, ctx)
    return ObjectStorageItem(
        name=str(item["name"]),
        capacity_gb=_validate_pos_number(item["capacity_gb"], context=f"{ctx}.capacity_gb"),
        tier=_validate_choice(
            item.get("tier", "hot"), VALID_OBJECT_TIERS, context=f"{ctx}.tier"
        ),
        quantity=_validate_pos_int(item.get("quantity", 1), context=f"{ctx}.quantity"),
    )


def _parse_database(item: dict, ctx: str) -> DatabaseItem:
    _require_keys(item, {"name", "vcpus", "memory_gb"}, ctx)
    return DatabaseItem(
        name=str(item["name"]),
        vcpus=_validate_pos_int(item["vcpus"], context=f"{ctx}.vcpus"),
        memory_gb=_validate_pos_number(item["memory_gb"], context=f"{ctx}.memory_gb"),
        storage_gb=_validate_nonneg_number(
            item.get("storage_gb", 0), context=f"{ctx}.storage_gb"
        ),
        engine=_validate_choice(
            item.get("engine", "postgres"), VALID_DB_ENGINES, context=f"{ctx}.engine"
        ),
        quantity=_validate_pos_int(item.get("quantity", 1), context=f"{ctx}.quantity"),
    )


def _parse_egress(item: dict, ctx: str) -> EgressItem:
    _require_keys(item, {"name", "gb_per_month"}, ctx)
    return EgressItem(
        name=str(item["name"]),
        gb_per_month=_validate_nonneg_number(
            item["gb_per_month"], context=f"{ctx}.gb_per_month"
        ),
        direction=_validate_choice(
            item.get("direction", "out_to_internet"),
            VALID_EGRESS_DIRECTIONS,
            context=f"{ctx}.direction",
        ),
    )


# --- Validation primitives ---


def _parse_list(raw: Any, parser, section: str) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InventoryError(
            f"{section} must be a list, got {type(raw).__name__}"
        )
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InventoryError(
                f"{section}[{i}] must be a mapping, got {type(item).__name__}"
            )
        out.append(parser(item, ctx=f"{section}[{i}]"))
    return out


def _require_keys(item: dict, required: set[str], ctx: str) -> None:
    missing = required - set(item.keys())
    if missing:
        raise InventoryError(
            f"{ctx} is missing required field(s): {', '.join(sorted(missing))}"
        )


def _validate_cloud(value: Any, context: str) -> str:
    if not isinstance(value, str) or value.lower() not in {"aws", "azure", "gcp", "oci"}:
        raise InventoryError(
            f"{context} must be one of aws / azure / gcp / oci, got {value!r}"
        )
    return value.lower()


def _validate_choice(value: Any, choices: set[str], context: str) -> str:
    if not isinstance(value, str) or value not in choices:
        choices_list = ", ".join(sorted(choices))
        raise InventoryError(
            f"{context} must be one of [{choices_list}], got {value!r}"
        )
    return value


def _validate_pos_int(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InventoryError(
            f"{context} must be a positive integer (>= 1), got {value!r}"
        )
    return value


def _validate_nonneg_int(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InventoryError(
            f"{context} must be a non-negative integer (>= 0), got {value!r}"
        )
    return value


def _validate_pos_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise InventoryError(
            f"{context} must be a positive number (> 0), got {value!r}"
        )
    return float(value)


def _validate_nonneg_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise InventoryError(
            f"{context} must be a non-negative number (>= 0), got {value!r}"
        )
    return float(value)


def _validate_unit_interval(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise InventoryError(
            f"{context} must be a number in [0.0, 1.0], got {value!r}"
        )
    return float(value)


def _validate_bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise InventoryError(
            f"{context} must be a boolean (true/false), got {value!r}"
        )
    return value
