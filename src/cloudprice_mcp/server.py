import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__
from .compare import compare_all_clouds
from .pricing import Cloud, load_catalog

server: Server = Server("cloudprice-mcp")


def _list_skus(cloud: Cloud) -> list[str]:
    catalog = load_catalog()
    return sorted(i.sku for i in catalog.by_cloud(cloud))


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
                "target spec (vCPUs and memory). Returns the best-fit SKU per cloud "
                "sorted by monthly cost, plus the absolute and percent savings of "
                "the cheapest vs the most expensive option."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vcpus": {
                        "type": "integer",
                        "description": "Minimum vCPUs required (e.g. 4)",
                        "minimum": 1,
                    },
                    "memory_gb": {
                        "type": "number",
                        "description": "Minimum memory in GiB (e.g. 16)",
                        "minimum": 0.5,
                    },
                },
                "required": ["vcpus", "memory_gb"],
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


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "get_aws_price":
        return _lookup("aws", "instance_type", arguments["instance_type"])

    if name == "get_azure_price":
        return _lookup("azure", "vm_size", arguments["vm_size"])

    if name == "get_gcp_price":
        return _lookup("gcp", "machine_type", arguments["machine_type"])

    if name == "compare_clouds":
        catalog = load_catalog()
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
    _ = __version__  # silence unused-import warnings on minimal builds
    asyncio.run(_run())


if __name__ == "__main__":
    main()
