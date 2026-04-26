import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__
from .compare import (
    ComputeRequest,
    StorageRequest,
    bulk_compare_compute,
    bulk_compare_storage,
    compare_all_clouds,
    compare_workload,
)
from .pricing import HOURS_PER_MONTH, Cloud, load_catalog

server: Server = Server("cloudprice-mcp")


def _list_skus(cloud: Cloud) -> list[str]:
    catalog = load_catalog()
    return sorted(i.sku for i in catalog.by_cloud(cloud))


_COMPUTE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly label for this row, e.g. 'web-tier-1'"},
        "vcpus": {"type": "integer", "minimum": 1},
        "memory_gb": {"type": "number", "minimum": 0.5},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "hours_per_month": {"type": "integer", "minimum": 1, "default": HOURS_PER_MONTH},
        "tier": {"type": ["string", "null"], "description": "Optional grouping label (e.g. Web/App/DB)"},
        "group": {"type": ["string", "null"], "description": "Optional sub-grouping label"},
        "os_disk_gb": {"type": ["number", "null"], "minimum": 0},
        "os_disk_type": {"type": "string", "enum": ["ssd", "hdd"], "default": "ssd"},
    },
    "required": ["name", "vcpus", "memory_gb"],
    "additionalProperties": False,
}

_STORAGE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly label for this volume, e.g. 'db-data-1'"},
        "capacity_gb": {"type": "number", "minimum": 1},
        "disk_type": {"type": "string", "enum": ["ssd", "hdd"], "default": "ssd"},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "tier": {"type": ["string", "null"]},
        "group": {"type": ["string", "null"]},
        "iops": {"type": ["integer", "null"], "minimum": 0, "description": "Carried as metadata; not used for SKU matching in v0.2"},
        "throughput_mbs": {"type": ["number", "null"], "minimum": 0, "description": "Carried as metadata; not used for SKU matching in v0.2"},
        "snapshot_count": {"type": "integer", "minimum": 0, "default": 0, "description": "Declared but not priced in v0.2 (you'll get a note in the response)"},
    },
    "required": ["name", "capacity_gb"],
    "additionalProperties": False,
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_aws_price",
            description=(
                "Look up the on-demand Linux hourly + monthly price for an AWS EC2 "
                "instance type in us-east-1. Returns vCPUs, memory, hourly USD, and "
                "monthly USD (730 hours)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_type": {
                        "type": "string",
                        "description": f"EC2 instance type, e.g. 't3.medium'. Available: {', '.join(_list_skus('aws'))}",
                    }
                },
                "required": ["instance_type"],
            },
        ),
        Tool(
            name="get_azure_price",
            description=(
                "Look up the on-demand Linux hourly + monthly price for an Azure VM "
                "size in eastus. Returns vCPUs, memory, hourly USD, and monthly USD."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vm_size": {
                        "type": "string",
                        "description": f"Azure VM size, e.g. 'D4s_v5'. Available: {', '.join(_list_skus('azure'))}",
                    }
                },
                "required": ["vm_size"],
            },
        ),
        Tool(
            name="get_gcp_price",
            description=(
                "Look up the on-demand Linux hourly + monthly price for a GCP Compute "
                "Engine machine type in us-east1. Returns vCPUs, memory, hourly USD, "
                "and monthly USD."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "machine_type": {
                        "type": "string",
                        "description": f"GCP machine type, e.g. 'e2-standard-4'. Available: {', '.join(_list_skus('gcp'))}",
                    }
                },
                "required": ["machine_type"],
            },
        ),
        Tool(
            name="compare_clouds",
            description=(
                "Find the cheapest equivalent VM across AWS, Azure, and GCP for a "
                "single target spec (vCPUs and memory). Returns the best-fit SKU per "
                "cloud sorted by monthly cost, plus the absolute and percent savings "
                "of the cheapest vs the most expensive option."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vcpus": {"type": "integer", "minimum": 1},
                    "memory_gb": {"type": "number", "minimum": 0.5},
                },
                "required": ["vcpus", "memory_gb"],
            },
        ),
        Tool(
            name="compare_compute_inventory",
            description=(
                "Bulk-compare a list of compute workloads across AWS, Azure, and GCP. "
                "Each row is independently sized to the cheapest VM that meets its "
                "vCPU/memory spec on each cloud, multiplied by quantity and "
                "hours_per_month. Optional os_disk_gb adds attached storage cost. "
                "Returns per-row matches, per-cloud totals, and the cheapest cloud "
                "overall. Useful for sizing-sheet style inputs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workloads": {
                        "type": "array",
                        "items": _COMPUTE_ITEM_SCHEMA,
                        "minItems": 1,
                    }
                },
                "required": ["workloads"],
            },
        ),
        Tool(
            name="compare_storage_inventory",
            description=(
                "Bulk-compare a list of storage volumes across AWS, Azure, and GCP. "
                "Each row picks the cheapest SKU matching its disk_type (ssd or hdd) "
                "on each cloud, then prices it at capacity_gb × quantity. Returns "
                "per-row matches, per-cloud totals, and cheapest cloud. IOPS, "
                "throughput, and snapshots are accepted but not priced in v0.2."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "volumes": {
                        "type": "array",
                        "items": _STORAGE_ITEM_SCHEMA,
                        "minItems": 1,
                    }
                },
                "required": ["volumes"],
            },
        ),
        Tool(
            name="compare_workload",
            description=(
                "Combined compute + storage compare across AWS, Azure, and GCP. "
                "Pass a compute list and a storage list (either may be empty). "
                "Returns nested per-row breakdowns plus combined per-cloud totals "
                "and the overall cheapest cloud. Mirrors the structure of a "
                "two-sheet sizing workbook (compute BoM + storage BoM)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "compute": {
                        "type": "array",
                        "items": _COMPUTE_ITEM_SCHEMA,
                        "default": [],
                    },
                    "storage": {
                        "type": "array",
                        "items": _STORAGE_ITEM_SCHEMA,
                        "default": [],
                    },
                },
            },
        ),
    ]


def _ok(payload: dict | list) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": message}, indent=2))]


def _lookup(cloud: Cloud, sku_field: str, sku: str) -> list[TextContent]:
    catalog = load_catalog()
    instance = catalog.find(cloud, sku)
    if instance is None:
        return _err(
            f"Unknown {cloud.upper()} {sku_field} '{sku}'. "
            f"Available: {', '.join(_list_skus(cloud))}"
        )
    return _ok({"as_of": catalog.as_of, **instance.to_dict()})


def _build_compute_requests(items: list[dict[str, Any]]) -> list[ComputeRequest]:
    return [
        ComputeRequest(
            name=item["name"],
            vcpus=int(item["vcpus"]),
            memory_gb=float(item["memory_gb"]),
            quantity=int(item.get("quantity", 1)),
            hours_per_month=int(item.get("hours_per_month", HOURS_PER_MONTH)),
            tier=item.get("tier"),
            group=item.get("group"),
            os_disk_gb=float(item["os_disk_gb"]) if item.get("os_disk_gb") else None,
            os_disk_type=item.get("os_disk_type", "ssd"),
        )
        for item in items
    ]


def _build_storage_requests(items: list[dict[str, Any]]) -> list[StorageRequest]:
    return [
        StorageRequest(
            name=item["name"],
            capacity_gb=float(item["capacity_gb"]),
            disk_type=item.get("disk_type", "ssd"),
            quantity=int(item.get("quantity", 1)),
            tier=item.get("tier"),
            group=item.get("group"),
            iops=int(item["iops"]) if item.get("iops") is not None else None,
            throughput_mbs=float(item["throughput_mbs"]) if item.get("throughput_mbs") is not None else None,
            snapshot_count=int(item.get("snapshot_count", 0)),
        )
        for item in items
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    catalog = load_catalog()

    if name == "get_aws_price":
        return _lookup("aws", "instance_type", arguments["instance_type"])

    if name == "get_azure_price":
        return _lookup("azure", "vm_size", arguments["vm_size"])

    if name == "get_gcp_price":
        return _lookup("gcp", "machine_type", arguments["machine_type"])

    if name == "compare_clouds":
        vcpus = int(arguments["vcpus"])
        memory_gb = float(arguments["memory_gb"])
        matches = compare_all_clouds(catalog, vcpus, memory_gb)
        if not matches:
            return _err("No matches found in catalog.")
        cheapest = matches[0]
        priciest = matches[-1]
        spread = priciest.instance.monthly_usd - cheapest.instance.monthly_usd
        pct = (spread / priciest.instance.monthly_usd * 100) if priciest.instance.monthly_usd else 0
        return _ok(
            {
                "as_of": catalog.as_of,
                "request": {"vcpus": vcpus, "memory_gb": memory_gb},
                "matches": [m.to_dict() for m in matches],
                "summary": {
                    "cheapest_cloud": cheapest.cloud,
                    "monthly_savings_usd": round(spread, 2),
                    "monthly_savings_pct": round(pct, 1),
                },
            }
        )

    if name == "compare_compute_inventory":
        workloads = _build_compute_requests(arguments["workloads"])
        result = bulk_compare_compute(catalog, workloads)
        return _ok({"as_of": catalog.as_of, **result})

    if name == "compare_storage_inventory":
        volumes = _build_storage_requests(arguments["volumes"])
        result = bulk_compare_storage(catalog, volumes)
        return _ok({"as_of": catalog.as_of, **result})

    if name == "compare_workload":
        compute = _build_compute_requests(arguments.get("compute", []))
        storage = _build_storage_requests(arguments.get("storage", []))
        if not compute and not storage:
            return _err("compare_workload needs at least one of compute or storage to be non-empty.")
        result = compare_workload(catalog, compute, storage)
        return _ok({"as_of": catalog.as_of, **result})

    return _err(f"Unknown tool: {name}")


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Console entry point. Runs the MCP server over stdio."""
    _ = __version__
    asyncio.run(_run())


if __name__ == "__main__":
    main()
