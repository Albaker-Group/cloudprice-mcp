import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__, dispatch
from .finops.commitment import ALL_SCENARIOS
from .pricing import HOURS_PER_MONTH

# version: without it the SDK reports its own version (e.g. "1.30.0") as the
# server's. instructions: clients pass this to the model, so it knows when to
# reach for these tools before it has read 25 descriptions.
server: Server = Server(
    "cloudprice-mcp",
    version=__version__,
    instructions=(
        "Cloud and LLM pricing for AWS, Azure, GCP and OCI from a weekly-refreshed "
        "public catalogue; no cloud account needed. Use these tools for any question "
        "about what compute, storage, egress, GPUs, databases or LLM tokens cost, "
        "which provider is cheapest, commitment (RI / Savings Plan) break-even, "
        "migration or 3-year TCO, carbon, or how prices changed over time. Quote the "
        "catalogue date and the honest_gaps each result returns."
    ),
)


# --- v0.6 FinOps inventory schema (shared across all 4 FinOps tools) ---


_FINOPS_COMPUTE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly label, e.g. 'api-tier'"},
        "vcpus": {"type": "integer", "minimum": 1},
        "memory_gb": {"type": "number", "minimum": 0.5},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "multi_az": {"type": "boolean", "default": False},
        "os_disk_gb": {"type": ["number", "null"], "minimum": 0},
        "os_disk_type": {"type": "string", "enum": ["ssd", "hdd"], "default": "ssd"},
        "snapshot_count": {"type": "integer", "minimum": 0, "default": 0},
        "snapshot_incremental_factor": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "default": 1.0,
            "description": "1.0 = upper-bound, 0.3 = typical real-world incremental, 0.0 = exclude.",
        },
    },
    "required": ["name", "vcpus", "memory_gb"],
    "additionalProperties": False,
}

_FINOPS_STORAGE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "capacity_gb": {"type": "number", "minimum": 1},
        "disk_type": {"type": "string", "enum": ["ssd", "hdd"], "default": "ssd"},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "snapshot_count": {"type": "integer", "minimum": 0, "default": 0},
        "snapshot_incremental_factor": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "default": 1.0,
        },
    },
    "required": ["name", "capacity_gb"],
    "additionalProperties": False,
}

_FINOPS_OBJECT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "capacity_gb": {"type": "number", "minimum": 1},
        "tier": {"type": "string", "enum": ["hot", "cool", "archive"], "default": "hot"},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
    },
    "required": ["name", "capacity_gb"],
    "additionalProperties": False,
}

_FINOPS_DATABASE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "vcpus": {"type": "integer", "minimum": 1},
        "memory_gb": {"type": "number", "minimum": 0.5},
        "storage_gb": {"type": "number", "minimum": 0, "default": 0},
        "engine": {"type": "string", "enum": ["postgres"], "default": "postgres"},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
    },
    "required": ["name", "vcpus", "memory_gb"],
    "additionalProperties": False,
}

_FINOPS_EGRESS_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "gb_per_month": {"type": "number", "minimum": 0},
        "direction": {
            "type": "string",
            "enum": ["out_to_internet", "inter_region"],
            "default": "out_to_internet",
        },
    },
    "required": ["name", "gb_per_month"],
    "additionalProperties": False,
}

_FINOPS_ONE_TIME_SCHEMA = {
    "type": "object",
    "properties": {
        "data_to_migrate_gb": {"type": "number", "minimum": 0, "default": 0},
    },
    "additionalProperties": False,
}


def _finops_inventory_properties(
    *, include_source_cloud: bool = True, include_commitment: bool = True
) -> dict:
    """Build the shared inventory properties dict reused across FinOps tool schemas."""
    props: dict = {
        "compute": {"type": "array", "items": _FINOPS_COMPUTE_ITEM_SCHEMA, "default": []},
        "storage": {"type": "array", "items": _FINOPS_STORAGE_ITEM_SCHEMA, "default": []},
        "object_storage": {"type": "array", "items": _FINOPS_OBJECT_ITEM_SCHEMA, "default": []},
        "databases": {"type": "array", "items": _FINOPS_DATABASE_ITEM_SCHEMA, "default": []},
        "egress": {"type": "array", "items": _FINOPS_EGRESS_ITEM_SCHEMA, "default": []},
        "multi_az": {"type": "boolean", "default": False},
        "one_time": _FINOPS_ONE_TIME_SCHEMA,
    }
    if include_source_cloud:
        props["source_cloud"] = {
            "type": "string",
            "enum": ["aws", "azure", "gcp", "oci"],
            "description": "Cloud the workload currently runs on.",
        }
    if include_commitment:
        props["commitment"] = {
            "type": "string",
            "enum": list(ALL_SCENARIOS),
            "default": "none",
        }
    return props


_COMPUTE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly label for this row, e.g. 'web-tier-1'"},
        "vcpus": {"type": "integer", "minimum": 1},
        "memory_gb": {"type": "number", "minimum": 0.5},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "hours_per_month": {"type": "integer", "minimum": 1, "default": HOURS_PER_MONTH},
        "tier": {
            "type": ["string", "null"],
            "description": "Optional grouping label (e.g. Web/App/DB)",
        },
        "group": {"type": ["string", "null"], "description": "Optional sub-grouping label"},
        "os_disk_gb": {"type": ["number", "null"], "minimum": 0},
        "os_disk_type": {"type": "string", "enum": ["ssd", "hdd"], "default": "ssd"},
        "os_disk_snapshot_count": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": "Number of OS-disk snapshots retained. Each priced at the cloud's snapshot per-GB rate × disk size × instance quantity.",
        },
        "os_disk_snapshot_incremental_factor": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "default": 1.0,
            "description": "Multiplier on OS-disk snapshot upper-bound cost. 1.0 = full, 0.3 = typical incremental, 0.0 = exclude. Defaults to 1.0.",
        },
    },
    "required": ["name", "vcpus", "memory_gb"],
    "additionalProperties": False,
}

_EGRESS_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Friendly label, e.g. 'api-egress' or 'cdn-bandwidth'",
        },
        "gb_per_month": {
            "type": "number",
            "minimum": 0,
            "description": "Data transfer volume in GB/month",
        },
        "direction": {
            "type": "string",
            "enum": ["out_to_internet", "inter_region"],
            "default": "out_to_internet",
            "description": "out_to_internet: outbound to public internet (honors free tier — AWS/Azure 100 GB, OCI 10 TB). inter_region: cross-region transfer within the same cloud (no free tier, flat rate ~$0.02/GB on hyperscalers, $0.0085 on OCI).",
        },
        "tier_label": {"type": ["string", "null"]},
    },
    "required": ["name", "gb_per_month"],
    "additionalProperties": False,
}


_OBJECT_STORAGE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Friendly label for this bucket/container, e.g. 'app-uploads'",
        },
        "capacity_gb": {"type": "number", "minimum": 1},
        "tier": {
            "type": "string",
            "enum": ["hot", "cool", "archive"],
            "default": "hot",
            "description": "Access tier: 'hot' = frequent (eg S3 Standard), 'cool' = infrequent, 'archive' = deep archive (eg Glacier)",
        },
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "tier_label": {"type": ["string", "null"], "description": "Optional grouping label"},
    },
    "required": ["name", "capacity_gb"],
    "additionalProperties": False,
}


_POSTGRES_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Friendly label for this database, e.g. 'orders-prod'",
        },
        "vcpus": {"type": "integer", "minimum": 1},
        "memory_gb": {"type": "number", "minimum": 0.5},
        "storage_gb": {
            "type": "number",
            "minimum": 0,
            "default": 0,
            "description": "Persistent storage size in GB",
        },
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "hours_per_month": {"type": "integer", "minimum": 1, "default": HOURS_PER_MONTH},
        "tier": {
            "type": ["string", "null"],
            "description": "Optional grouping label (e.g. Prod/Stage/Dev)",
        },
    },
    "required": ["name", "vcpus", "memory_gb"],
    "additionalProperties": False,
}


_STORAGE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Friendly label for this volume, e.g. 'db-data-1'",
        },
        "capacity_gb": {"type": "number", "minimum": 1},
        "disk_type": {"type": "string", "enum": ["ssd", "hdd"], "default": "ssd"},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "tier": {"type": ["string", "null"]},
        "group": {"type": ["string", "null"]},
        "iops": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": "Carried as metadata; not used for SKU matching in v0.2",
        },
        "throughput_mbs": {
            "type": ["number", "null"],
            "minimum": 0,
            "description": "Carried as metadata; not used for SKU matching in v0.2",
        },
        "snapshot_count": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": "Number of snapshots retained. Priced at the cloud's snapshot per-GB rate × capacity × volume quantity.",
        },
        "snapshot_incremental_factor": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "default": 1.0,
            "description": "Multiplier on the upper-bound snapshot cost. 1.0 = full upper-bound (each snapshot full capacity). 0.3 = typical real-world incremental dedup (~30%). 0.0 = exclude snapshots from total. Defaults to 1.0 for backward compatibility.",
        },
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
                "monthly USD (730 hours). For multi-cloud comparisons including OCI, "
                "Azure, and GCP, use compare_clouds instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_type": {
                        "type": "string",
                        "description": f"EC2 instance type, e.g. 't3.medium'. Available: {', '.join(dispatch.list_skus('aws'))}",
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
                        "description": f"Azure VM size, e.g. 'D4s_v5'. Available: {', '.join(dispatch.list_skus('azure'))}",
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
                        "description": f"GCP machine type, e.g. 'e2-standard-4'. Available: {', '.join(dispatch.list_skus('gcp'))}",
                    }
                },
                "required": ["machine_type"],
            },
        ),
        Tool(
            name="compare_clouds",
            description=(
                "Find the cheapest equivalent VM across AWS, Azure, GCP, and OCI for "
                "a single target spec (vCPUs and memory). Returns the best-fit SKU "
                "per cloud sorted by monthly cost, plus the absolute and percent "
                "savings of the cheapest vs the most expensive option. OCI A1 Always "
                "Free is included — for specs that fit within 4 OCPU + 24 GB Arm, "
                "OCI returns $0/mo (real perpetual free tier, not a quirk)."
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
                "Bulk-compare a list of compute workloads across AWS, Azure, GCP, and "
                "OCI. Each row is independently sized to the cheapest VM that meets "
                "its vCPU/memory spec on each cloud, multiplied by quantity and "
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
                "Bulk-compare a list of block-storage volumes across AWS, Azure, GCP, "
                "and OCI. Each row picks the cheapest SKU matching its disk_type "
                "(ssd or hdd) on each cloud, then prices it at capacity_gb × quantity. "
                "Returns per-row matches, per-cloud totals, and cheapest cloud. IOPS "
                "and throughput are accepted but not used for SKU matching. Snapshot "
                "pricing is upper-bound (real-world incremental snapshots cost less)."
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
            name="compare_egress",
            description=(
                "Compare data-transfer costs across AWS, Azure, GCP, and OCI for a "
                "given monthly volume. Two directions supported: 'out_to_internet' "
                "(tiered pricing with free-tier credits — AWS/Azure 100 GB, OCI 10 TB "
                "free) and 'inter_region' (flat rate for cross-region transfer within "
                "the same cloud). At 50 TB/mo of internet egress OCI is ~12× cheaper "
                "than the hyperscalers — a real competitive moat for content/CDN "
                "workloads. VPC peering is NOT yet modeled."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transfers": {
                        "type": "array",
                        "items": _EGRESS_ITEM_SCHEMA,
                        "minItems": 1,
                    }
                },
                "required": ["transfers"],
            },
        ),
        Tool(
            name="compare_object_storage",
            description=(
                "Compare object-storage pricing across AWS S3, Azure Blob, GCP Cloud "
                "Storage, and OCI Object Storage. Each request specifies capacity_gb "
                "and access tier (hot/cool/archive); the tool picks the cheapest SKU "
                "per cloud at that tier. OCI offers 20 GB Always Free in the 'hot' "
                "tier — surfaced when capacity fits. NOTE: egress, request, and "
                "retrieval costs are not modeled (often the actual hidden killer). "
                "Prices are hand-entered list prices (as of 2026-05-04), not part of the "
                "weekly refresh — verify against the provider's page before deciding."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "volumes": {
                        "type": "array",
                        "items": _OBJECT_STORAGE_ITEM_SCHEMA,
                        "minItems": 1,
                    }
                },
                "required": ["volumes"],
            },
        ),
        Tool(
            name="compare_postgres_database",
            description=(
                "Compare managed PostgreSQL pricing across AWS RDS, Azure Database for "
                "PostgreSQL, GCP Cloud SQL, and OCI Database with PostgreSQL. Each "
                "request specifies vCPUs, memory, and storage_gb; the tool picks the "
                "cheapest matching SKU per cloud and totals compute + storage. "
                "Prices are hand-entered estimates (as of 2026-05-04), not part of the "
                "weekly refresh — verify against the provider's page before deciding."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "databases": {
                        "type": "array",
                        "items": _POSTGRES_ITEM_SCHEMA,
                        "minItems": 1,
                    }
                },
                "required": ["databases"],
            },
        ),
        Tool(
            name="compare_workload",
            description=(
                "Combined compute + block-storage compare across AWS, Azure, GCP, "
                "and OCI. Pass a compute list and a storage list (either may be "
                "empty). Returns nested per-row breakdowns plus combined per-cloud "
                "totals and the overall cheapest cloud. Mirrors the structure of a "
                "two-sheet sizing workbook (compute BoM + storage BoM). Optional "
                "`commitment` parameter estimates 1-year or 3-year Reserved Instance "
                "/ Savings Plan / Committed Use discount on compute (storage stays "
                "at on-demand). For object storage, use compare_object_storage. "
                "For managed databases, use compare_postgres_database."
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
                    "commitment": {
                        "type": "string",
                        "enum": ["none", "1yr_no_upfront", "3yr_partial_upfront"],
                        "default": "none",
                        "description": "Compute commitment tier. 'none' = on-demand only. '1yr_no_upfront' applies a representative 30% compute discount. '3yr_partial_upfront' applies 50%. Storage and snapshots are not discounted.",
                    },
                    "multi_az": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, double compute cost on every cloud to model Multi-AZ / HA deployments (sync replicas across two zones). Storage stays at 1x because object/block storage is usually cross-AZ at base price already.",
                    },
                },
            },
        ),
        # --- v0.6 FinOps decision tools ---
        Tool(
            name="assess_migration",
            description=(
                "Project cross-cloud cost + payback for moving a workload away from "
                "its source cloud. Inputs: source_cloud + workload inventory (compute / "
                "storage / object_storage / databases / egress) + optional one_time data "
                "to migrate. Returns per-target monthly cost, savings %, exit egress cost, "
                "payback months, ranked recommendation by 3-year TCO, and triggered caveats "
                "(e.g., 'OCI A1.Flex is ARM — verify your AMIs'). The kind of FinOps "
                "decision that normally lives in a half-built spreadsheet — now one tool call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_finops_inventory_properties(),
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Target clouds to evaluate. Default: all clouds except source_cloud.",
                    },
                },
                "required": ["source_cloud"],
            },
        ),
        Tool(
            name="optimize_commitment",
            description=(
                "Compute per-scenario cost / savings / payback for compute commitment "
                "options (none, 1yr_no_upfront, 1yr_all_upfront, 3yr_no_upfront, "
                "3yr_partial_upfront, 3yr_all_upfront). Returns each scenario's monthly "
                "cost, upfront, 3-year total, savings %, and payback months — plus the "
                "recommended scenario by lowest 3-year TCO. Compute-only (storage / "
                "database / object / egress are not discounted because most clouds don't "
                "offer meaningful commitments on these). Per-family RI tiers come in "
                "v0.6.x; v0.6.0 uses cloud-level conservative averages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_finops_inventory_properties(include_commitment=False),
                    "cloud": {
                        "type": "string",
                        "enum": ["aws", "azure", "gcp", "oci"],
                        "description": "Cloud to evaluate (default: source_cloud, then 'aws').",
                    },
                    "scenarios": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(ALL_SCENARIOS)},
                        "description": "Subset of commitment scenarios to evaluate. Default: all 6.",
                    },
                },
                "required": ["compute"],
            },
        ),
        Tool(
            name="compare_total_cost_of_ownership",
            description=(
                "Project per-cloud per-year cost over a configurable horizon (default 3 "
                "years), with linear YoY growth assumptions for compute / storage / egress. "
                "Returns cumulative TCO per cloud, year-by-year breakdown by category, and "
                "sensitivity analysis identifying the most impactful growth variable. The "
                "kind of number that goes into board decks and budget conversations — now "
                "computed from a public catalog instead of a spreadsheet."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_finops_inventory_properties(include_source_cloud=False),
                    "horizon_years": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 3,
                        "description": "Years to project (default 3 — FinOps standard).",
                    },
                    "growth": {
                        "type": "object",
                        "properties": {
                            "compute_pct_yoy": {
                                "type": "number",
                                "default": 0.0,
                                "description": "+0.20 means +20% YoY compute growth.",
                            },
                            "storage_pct_yoy": {"type": "number", "default": 0.0},
                            "egress_pct_yoy": {"type": "number", "default": 0.0},
                        },
                        "additionalProperties": False,
                    },
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Clouds to project. Default: all 4 clouds.",
                    },
                },
            },
        ),
        Tool(
            name="find_egress_arbitrage",
            description=(
                "Specialized assess_migration scoped to egress patterns. Useful when a "
                "team's largest cost line is data transfer (CDN workloads, video streaming, "
                "content distribution). Returns per-target egress cost, monthly + annual "
                "savings, payback months on any one-time exit cost, and recommendation. "
                "The OCI 12× moat is the headline finding: at 50 TB/month internet egress, "
                "OCI is roughly $340 vs $4,000+ on the hyperscalers because of OCI's "
                "10 TB/month free tier + $0.0085/GB beyond."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_cloud": {
                        "type": "string",
                        "enum": ["aws", "azure", "gcp", "oci"],
                    },
                    "egress": {
                        "type": "array",
                        "items": _FINOPS_EGRESS_ITEM_SCHEMA,
                        "minItems": 1,
                    },
                    "one_time": _FINOPS_ONE_TIME_SCHEMA,
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                    },
                },
                "required": ["source_cloud", "egress"],
            },
        ),
        # --- v0.7.1 price-history tools ---
        Tool(
            name="get_price_history",
            description=(
                "Return the multi-cloud price history for a specific (cloud, sku). "
                "cloudprice-mcp persists every weekly auto-refresh as a dated snapshot, "
                "so this is the only FinOps tool that can answer 'what did m5.xlarge "
                "cost in May?' — neither AWS Calculator nor GCP Estimator preserves "
                "historical pricing. Returns the timeseries, total change %, and the "
                "earliest/latest data points. Useful for: validating commitment timing, "
                "spotting quiet price increases, citing audit-traceable historical "
                "numbers to a CFO."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cloud": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                    "sku": {
                        "type": "string",
                        "description": "Instance type / SKU name, e.g. m5.xlarge, D2s_v5, e2-standard-2, VM.Standard.E5.Flex.1OCPU",
                    },
                    "since": {
                        "type": ["string", "null"],
                        "description": "Optional ISO date (YYYY-MM-DD); restricts to snapshots on or after this date.",
                    },
                },
                "required": ["cloud", "sku"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="list_tracked_skus",
            description=(
                "List every (cloud, sku) pair present in the bundled price-history "
                "dataset, with how many weekly snapshots exist for each. Use this to "
                "discover which SKUs you can call get_price_history on, or to find "
                "the biggest-mover SKUs since a given date. Filter by cloud to scope."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cloud": {
                        "type": ["string", "null"],
                        "enum": ["aws", "azure", "gcp", "oci", None],
                    },
                    "since": {
                        "type": ["string", "null"],
                        "description": "Optional ISO date; only consider snapshots on or after this date.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        # --- v0.14.0 FinOps decision report (CFO-grade markdown) ---
        Tool(
            name="generate_decision_report",
            description=(
                "Generate a CFO-grade FinOps decision report in markdown. "
                "Combines assess_migration + compare_carbon_footprint into a "
                "single structured artifact the user can paste into a wiki / "
                "PR / Slack / email. Sections: executive summary, per-cloud "
                "cost table, carbon comparison (optional), the workload spec, "
                "aggregated honest_gaps, and an audit trail (package version, "
                "catalog as_of, requester, reproduce command). The 'trust' "
                "angle commercial FinOps tools can't easily replicate because "
                "their reports live in their UI; this one is markdown the "
                "user owns. Use when the question is 'we need a decision "
                "artifact a CFO can sign off on'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_finops_inventory_properties(),
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Optional list of target clouds to compare against the source.",
                    },
                    "include_carbon": {"type": "boolean", "default": True},
                    "requester": {
                        "type": "string",
                        "description": "Optional name/email for the audit trail.",
                    },
                },
                "required": ["source_cloud"],
                "additionalProperties": False,
            },
        ),
        # --- v0.13.0 Price anomaly detection ---
        Tool(
            name="detect_price_anomalies",
            description=(
                "Statistical anomaly detection over the bundled price-history "
                "dataset. Compounds the v0.7.1 history archive: as snapshots "
                "accumulate weekly, this tool auto-flags unusual price moves "
                "without you having to manually inspect every SKU. Two methods, "
                "auto-selected by dataset density: percent_change (for <5 "
                "snapshots) and z_score (>=5). Sensitivities: strict (10% / "
                "z>=2.5), moderate (5% / z>=2.0, default), permissive (2% / z>=1.5). "
                "Use for 'what changed this week?' and 'anything weird across "
                "any cloud since {date}?' queries. Excellent source for "
                "weekly LinkedIn / blog content auto-generation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "ISO date — restrict to snapshots on or after this date. Optional.",
                    },
                    "cloud": {
                        "type": "string",
                        "enum": ["aws", "azure", "gcp", "oci"],
                        "description": "Filter to one cloud. Optional.",
                    },
                    "sensitivity": {
                        "type": "string",
                        "enum": ["strict", "moderate", "permissive"],
                        "default": "moderate",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["percent_change", "z_score", "auto"],
                        "default": "auto",
                    },
                    "limit": {"type": "integer", "minimum": 1, "default": 25},
                },
                "additionalProperties": False,
            },
        ),
        # --- v0.12.0 LLM token pricing ---
        Tool(
            name="compare_token_pricing",
            description=(
                "Cross-provider LLM token pricing comparison — the only FinOps tool "
                "that returns per-1M input/output rates AND monthly cost for the same "
                "model on each provider that hosts it (Claude on Anthropic vs Bedrock "
                "vs Vertex; GPT on OpenAI vs Azure OpenAI; Llama on Bedrock; Gemini "
                "on Google vs Vertex; Mistral / DeepSeek direct). Filter by model_family "
                "('claude', 'gpt', 'gemini', 'llama', 'mistral', 'deepseek'), exact "
                "model_id, or provider list. If monthly_input_tokens + monthly_output_tokens "
                "are provided, ranks by total monthly USD. Otherwise ranks by per-1M "
                "output cost (output tokens dominate most workloads). Surfaces cache_read "
                "/ cache_write rates when published — most workloads see 30-90% input cost "
                "reduction with proper prompt caching."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "model_family": {
                        "type": "string",
                        "enum": ["claude", "gpt", "gemini", "llama", "mistral", "deepseek"],
                        "description": "Filter to one model family. Optional.",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "Exact model ID (e.g. 'claude-4-sonnet', 'gpt-4o', 'gemini-2.0-flash'). Optional.",
                    },
                    "providers": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "anthropic",
                                "openai",
                                "google",
                                "deepseek",
                                "mistral",
                                "bedrock",
                                "vertex",
                                "azure_openai",
                            ],
                        },
                        "description": "Optional list of providers to include.",
                    },
                    "monthly_input_tokens": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Monthly input token volume. Required to get monthly_total_usd in result.",
                    },
                    "monthly_output_tokens": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Monthly output token volume. Required to get monthly_total_usd in result.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        # --- v0.18.0 FOCUS 1.3 export ---
        Tool(
            name="analyze_bill",
            description=(
                "Analyse the user's actual cloud bill from a FOCUS 1.x CSV export "
                "(AWS Data Exports, Azure Cost Management, GCP BigQuery FOCUS view, OCI). "
                "Read from local disk only; nothing is sent anywhere. Returns total cost, "
                "top cost drivers grouped by service / category / provider / region / "
                "SKU / resource, cost by month, the biggest month-over-month movers, "
                "list vs effective cost (what discounts saved), and commitment coverage "
                "of usage when the export carries commitment columns. Use for: 'where is "
                "our cloud money going', 'what jumped last month', 'how much of our "
                "usage is covered by RIs / Savings Plans'. Ask the user for the path to "
                "their export file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Local path to a FOCUS export, .csv or .csv.gz.",
                    },
                    "group_by": {
                        "type": "string",
                        "enum": [
                            "ServiceName",
                            "ServiceCategory",
                            "ProviderName",
                            "RegionId",
                            "SkuId",
                            "ResourceId",
                        ],
                        "default": "ServiceName",
                    },
                    "top_n": {"type": "integer", "minimum": 1, "default": 10},
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="find_commitment_gaps",
            description=(
                "FinOps rate optimisation on the user's actual bill (a local FOCUS 1.x CSV "
                "export; nothing is sent anywhere). Returns commitment coverage of "
                "eligible compute and database usage, utilisation of existing Savings "
                "Plans / RIs / CUDs (and what unused commitment cost), and per cloud the "
                "steady on-demand spend that could be committed, with the $/hour "
                "commitment and annual saving for 1-year and 3-year no-upfront terms. "
                "Use for: 'are we under-committed', 'how much would a Savings Plan "
                "save us', 'are our reservations being used'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Local path to a FOCUS export, .csv or .csv.gz.",
                    },
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="detect_bill_anomalies",
            description=(
                "FinOps anomaly management on the user's actual bill (a local FOCUS 1.x "
                "CSV export; nothing is sent anywhere). Compares each service's daily (or "
                "monthly) cost with its own recent normal and reports spikes, drops and "
                "new spend as events, ranked by excess cost, flagging events still "
                "going in the latest period. Use for: 'what blew up our bill', 'did "
                "anything unusual happen last week', 'what new spend appeared'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Local path to a FOCUS export, .csv or .csv.gz.",
                    },
                    "group_by": {
                        "type": "string",
                        "enum": [
                            "ServiceName",
                            "ServiceCategory",
                            "ProviderName",
                            "RegionId",
                            "SkuId",
                            "ResourceId",
                        ],
                        "default": "ServiceName",
                    },
                    "sensitivity": {
                        "type": "string",
                        "enum": ["strict", "moderate", "permissive"],
                        "default": "moderate",
                    },
                    "min_impact": {
                        "type": "number",
                        "minimum": 0,
                        "default": 10,
                        "description": "Ignore moves smaller than this, in billing currency.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "default": 10},
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="analyze_cost_allocation",
            description=(
                "FinOps allocation on the user's actual bill (a local FOCUS 1.x CSV "
                "export; nothing is sent anywhere). Measures how much allocatable cost "
                "carries the tags used for showback / chargeback, coverage per required "
                "tag, cost by the values of one tag (e.g. team), cost by sub-account, "
                "and the biggest untagged services and resources. Use for: 'how much of "
                "our spend is tagged', 'cost per team', 'what is missing a cost-center "
                "tag'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Local path to a FOCUS export, .csv or .csv.gz.",
                    },
                    "required_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tag keys every resource should carry, e.g. "
                        "['team', 'env']. Omit to count any tag as tagged.",
                    },
                    "tag_key": {
                        "type": "string",
                        "description": "Break cost down by the values of this tag.",
                    },
                    "top_n": {"type": "integer", "minimum": 1, "default": 10},
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="forecast_spend",
            description=(
                "FinOps forecasting on the user's actual bill (a local FOCUS 1.x CSV "
                "export; nothing is sent anywhere). Projects month-end spend and the "
                "next months from the trend in recurring spend, each with a 95% range, "
                "and lists the services whose spend is growing fastest. Daily exports "
                "(28+ days) give a daily trend and a month-end estimate. Use for: "
                "'what will this month's bill be', 'where is spend heading next "
                "quarter', 'what is driving growth'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Local path to a FOCUS export, .csv or .csv.gz.",
                    },
                    "horizon_months": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 12,
                        "default": 3,
                    },
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="calculate_unit_cost",
            description=(
                "FinOps unit economics on the user's actual bill (a local FOCUS 1.x CSV "
                "export; nothing is sent anywhere). Divides monthly recurring spend by "
                "business volumes the user supplies (customers, requests, orders) to "
                "give cost per unit by month, and whether spend is growing faster or "
                "slower than the business. Optionally scoped to one product or team by "
                "tag. Use for: 'cost per customer', 'are we scaling efficiently'. Ask "
                "the user for the monthly volumes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Local path to a FOCUS export, .csv or .csv.gz.",
                    },
                    "units": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "month": {"type": "string", "description": "YYYY-MM"},
                                "units": {"type": "number", "exclusiveMinimum": 0},
                            },
                            "required": ["month", "units"],
                        },
                        "description": "Business volume per month, e.g. "
                        "[{'month': '2026-10', 'units': 120000}].",
                    },
                    "unit_name": {
                        "type": "string",
                        "default": "unit",
                        "description": "What a unit is, e.g. 'customer' or 'request'.",
                    },
                    "tag_key": {
                        "type": "string",
                        "description": "Scope cost to rows with this tag (with tag_value).",
                    },
                    "tag_value": {"type": "string"},
                },
                "required": ["file_path", "units"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="reprice_bill",
            description=(
                "FinOps benchmarking on the user's actual bill (a local FOCUS 1.x CSV "
                "export; nothing is sent anywhere). Works out the vCPU-hours and memory "
                "the bill's compute actually used and prices that same capacity at list "
                "price on AWS, Azure, GCP and OCI, per instance type and in total, next "
                "to what the user really paid. Reports what it could not reprice (GPUs, "
                "licensed OS, unknown types) with its cost. Use for: 'what would our "
                "bill cost on another cloud', 'are we on the cheapest cloud for this "
                "compute', 'is a migration worth looking at'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Local path to a FOCUS export, .csv or .csv.gz.",
                    },
                    "top_n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 10,
                        "description": "How many workload lines to return, largest first.",
                    },
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="analyze_ai_spend",
            description=(
                "FinOps for AI on the user's actual bill (a local FOCUS 1.x CSV export; "
                "nothing is sent anywhere). Finds the generative-AI lines (Amazon "
                "Bedrock, Azure OpenAI / AI Foundry, Vertex AI, OCI Generative AI) and "
                "gives spend per model split into input, output, cache-read and "
                "cache-write tokens; what was paid against list price; the same model's "
                "cost on other providers for the same tokens; how much input comes from "
                "prompt cache and what more would save; and list price changes for "
                "those models from the weekly LLM price archive. Use for: 'what is our "
                "LLM spend', 'are we overpaying for this model', 'would caching help'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Local path to a FOCUS export, .csv or .csv.gz.",
                    },
                    "top_n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 10,
                        "description": "How many models to return, largest spend first.",
                    },
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="review_agentcore",
            description=(
                "Well-Architected review of an Amazon Bedrock AgentCore deployment, run "
                "locally on `terraform show -json` output (a saved plan or state; nothing "
                "is sent anywhere, no AWS credentials). Coded rules check every Runtime, "
                "Harness and Gateway and the IAM, logs and alarms around them: public "
                "network, unauthenticated or debug gateways, InvokeModel on every model, "
                "secrets in environment variables, idle and lifetime settings, harness "
                "caps, log retention, alarms and cost tags. Each finding has severity, "
                "pillar, resource address, a Terraform fix and the AWS doc it rests on. "
                "Given a FOCUS bill (and a tag such as agent=genie) it adds what the agent "
                "costs by component and the prompt-caching and over-list-price savings. "
                "Use for: 'review our AgentCore setup', 'is this agent production-ready', "
                "'what does this agent cost and what should we fix'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "plan_path": {
                        "type": "string",
                        "description": "Local path to `terraform show -json` output (.json).",
                    },
                    "bill_path": {
                        "type": "string",
                        "description": "Optional local FOCUS export (.csv or .csv.gz).",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Optional key=value tag that marks this agent's "
                        "lines in the bill, e.g. agent=genie.",
                    },
                    "skips": {
                        "type": "array",
                        "description": "Findings accepted on purpose, reported as skipped.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rule": {"type": "string"},
                                "resource": {
                                    "type": "string",
                                    "description": "Resource address; omit for every resource.",
                                },
                                "reason": {"type": "string"},
                            },
                            "required": ["rule", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["plan_path"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="compare_llm_vs_selfhost",
            description=(
                "Pay per token or rent GPUs and serve the model yourself? Prices a "
                "monthly token volume on an LLM API, then the GPU servers needed to "
                "serve the same volume on AWS, Azure, GCP and OCI, and the break-even "
                "volume. Needs the measured generation throughput of one server "
                "(output tokens/second, e.g. from a vLLM benchmark): it decides the "
                "answer and is never guessed. Use for: 'should we self-host Llama 3.3 "
                "70B instead of paying Together', 'at what volume do H100s beat the "
                "DeepSeek API', 'cheapest cloud to serve our open model'. Self-host "
                "cost is GPU rental only; see honest_gaps."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "API model ID to price, as in the LLM catalogue, e.g. 'deepseek/deepseek-chat', 'together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo'.",
                    },
                    "api_provider": {
                        "type": "string",
                        "description": "Host for the API price. Default: first-party.",
                    },
                    "monthly_input_tokens": {"type": "integer", "minimum": 0},
                    "monthly_output_tokens": {"type": "integer", "minimum": 0},
                    "gpu_type": {
                        "type": "string",
                        "description": "GPU to self-host on: T4, L4, A10, A10G, L40S, V100, A100, H100.",
                    },
                    "gpu_count": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": "GPUs per server.",
                    },
                    "output_tokens_per_second": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "Measured aggregate generation throughput of ONE server of this size for this model, across concurrent requests.",
                    },
                    "utilization": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 1,
                        "default": 0.6,
                        "description": "Share of each hour the server is busy. Real traffic is bursty; 0.4-0.7 is typical.",
                    },
                    "prefill_speedup": {
                        "type": "number",
                        "minimum": 1,
                        "default": 10,
                        "description": "How much faster input (prefill) is processed than output is generated.",
                    },
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                    },
                },
                "required": ["model", "gpu_type", "output_tokens_per_second"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="compare_agent_platforms",
            description=(
                "Where to host an AI agent, and what a month of it costs: the same "
                "agent priced on Amazon Bedrock AgentCore Runtime, Azure AI Foundry "
                "Agent Service (prompt agents and hosted agents), Google Vertex AI "
                "Agent Engine, and plain serverless (AWS Lambda, Azure Container Apps). "
                "Each platform is split into hosting, tools, memory and model tokens, "
                "with the model priced on that cloud's own model service (or marked "
                "unavailable), and the busy share of a run below which AgentCore costs "
                "less than each alternative. Use for: 'AgentCore vs Foundry vs Vertex "
                "for our agent', 'should this agent run on Lambda or AgentCore', 'what "
                "will hosting 50,000 agent runs a month cost'. For token counts per run, "
                "use estimate_agent_cost first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "runs_per_month": {"type": "integer", "minimum": 1, "default": 10000},
                    "run_seconds": {
                        "type": "number",
                        "default": 30,
                        "description": "Wall-clock seconds per run, waiting included.",
                    },
                    "active_cpu_share": {
                        "type": "number",
                        "default": 0.2,
                        "description": "Share of the run the agent is busy, not waiting on the model or tools (0-1].",
                    },
                    "vcpu": {"type": "number", "default": 1},
                    "memory_gb": {"type": "number", "default": 2},
                    "tool_calls_per_run": {"type": "integer", "minimum": 0, "default": 5},
                    "memory_retrievals_per_run": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Long-term memory lookups per run (AgentCore Memory, Agent Engine Memory Bank).",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model to price on each cloud, e.g. 'claude-sonnet-5-5' or 'gpt-5'.",
                    },
                    "input_tokens_per_run": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Default: estimate_agent_cost's default 8-step run (97,200).",
                    },
                    "output_tokens_per_run": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Default: estimate_agent_cost's default 8-step run (3,200).",
                    },
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="estimate_agent_cost",
            description=(
                "Estimate what one run of an AI agent costs, and a month of runs. "
                "Models what per-call math misses: each agent step re-sends the whole "
                "conversation (system prompt, tool definitions, earlier outputs and tool "
                "results), so input tokens grow every step. Returns cost per run and per "
                "month, the per-step token breakdown, how much of the input is context "
                "growth, what prompt caching saves, and the same run priced on other "
                "current models (Claude, GPT, Gemini, DeepSeek by default). Use for: "
                "'what will my agent cost at 50k runs a month', 'is Sonnet or Haiku "
                "cheaper for this agent', 'how much does caching save', 'budget for an "
                "AgentCore / LangGraph / Agents SDK agent'. Defaults describe a typical "
                "8-step tool-using agent; pass measured token counts for a real figure."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "Model ID as in the LLM catalogue, e.g. 'claude-sonnet-5-5', 'gpt-5-mini', 'gemini/gemini-2.5-flash'. Find IDs with lookup_extended_model_pricing.",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Host to price on, e.g. 'anthropic', 'bedrock', 'vertex'. Default: the model's first-party provider.",
                    },
                    "steps": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 8,
                        "description": "LLM calls per run, including the final answer.",
                    },
                    "system_prompt_tokens": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 2000,
                        "description": "Tokens in the system prompt.",
                    },
                    "tool_definition_tokens": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 3000,
                        "description": "Tokens in the tool/function definitions sent with every call.",
                    },
                    "user_input_tokens": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 500,
                        "description": "Tokens in the user's request.",
                    },
                    "output_tokens_per_step": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 400,
                        "description": "Average model output tokens per step.",
                    },
                    "tool_result_tokens_per_step": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 1500,
                        "description": "Average tokens a tool returns per step.",
                    },
                    "runs_per_month": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 10000,
                        "description": "Agent runs per month.",
                    },
                    "compare_models": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Model IDs to price the same run on. Default: popular current Claude, GPT, Gemini and DeepSeek models.",
                    },
                    "hosting": {
                        "type": "object",
                        "description": "Add hosting to the token cost: prices the run on AgentCore Runtime (+ Gateway, Memory) and on Lambda, and returns full per-run and monthly totals. AgentCore bills CPU only while active; Lambda bills the whole run.",
                        "properties": {
                            "run_seconds": {
                                "type": "number",
                                "exclusiveMinimum": 0,
                                "description": "Wall-clock seconds per run, including waiting on the model.",
                            },
                            "vcpu": {"type": "number", "exclusiveMinimum": 0, "default": 1},
                            "memory_gb": {"type": "number", "exclusiveMinimum": 0, "default": 2},
                            "active_cpu_share": {
                                "type": "number",
                                "exclusiveMinimum": 0,
                                "maximum": 1,
                                "default": 0.2,
                                "description": "Share of the run the CPU is busy; the rest is I/O wait.",
                            },
                            "tool_calls_per_run": {
                                "type": "integer",
                                "minimum": 0,
                                "description": "Gateway tool invocations per run. Default: steps - 1.",
                            },
                            "memory_retrievals_per_run": {
                                "type": "integer",
                                "minimum": 0,
                                "default": 0,
                            },
                            "memory_events_per_run": {
                                "type": "integer",
                                "minimum": 0,
                                "default": 0,
                            },
                        },
                        "required": ["run_seconds"],
                        "additionalProperties": False,
                    },
                },
                "required": ["model"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="model_retirements",
            description=(
                "Which LLMs providers are retiring: models whose retirement date "
                "(from LiteLLM's feed, refreshed weekly) falls within the window, "
                "across Bedrock, Azure OpenAI / AI Foundry, Vertex AI, OpenAI, "
                "Anthropic and others, plus those retired in the last 90 days. For "
                "each: the date, days left, and the newest same-family model on the "
                "same provider that is not retiring, with its list price change. Use "
                "for: 'which of our models are being retired', 'is Claude 3.5 Sonnet "
                "on Bedrock going away', 'what replaces gpt-4o on Azure'. "
                "analyze_ai_spend and review_agentcore flag retiring models on your "
                "bill and in your deployment automatically."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "within_days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1095,
                        "default": 180,
                        "description": "Look this many days ahead.",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Catalogue provider, e.g. 'bedrock', 'azure_openai', 'vertex', 'openai'.",
                    },
                    "model": {
                        "type": "string",
                        "description": "A model name to check, e.g. 'claude 3.5 sonnet' or 'gpt-4o'.",
                    },
                    "top_n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                        "description": "How many models to return, soonest first.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="llm_price_changes",
            description=(
                "What changed in LLM token pricing between two weeks: models that "
                "were repriced (with old and new $/1M input and output, and % move), "
                "models added, and models removed, across ~3,600 model/provider rows "
                "from LiteLLM (direct provider prices) and OpenRouter (routed prices). "
                "Backed by a weekly archive kept since 2026-05-23. Use for: 'what got "
                "cheaper this week', 'did Anthropic or Bedrock change prices since "
                "October', 'new models on Groq this month', 'has Mistral Large's "
                "price moved'. Defaults to the latest week vs the week before."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "ISO date. Compare from the latest archived week on or before this date. Default: the week before 'until'.",
                    },
                    "until": {
                        "type": "string",
                        "description": "ISO date. Compare to the latest archived week on or before this date. Default: the latest week.",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["litellm", "openrouter"],
                        "description": "Restrict to one upstream. Default: both.",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Case-insensitive substring of the provider, e.g. 'bedrock', 'anthropic', 'groq'.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive substring of the model ID, e.g. 'claude', 'llama-3.3'.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "default": 25},
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="export_focus",
            description=(
                "Emit FOCUS 1.3-shaped rows from any pricing query, ready to "
                "import into Vantage Custom Providers / Microsoft Cost Mgmt / "
                "OpenCost / Apptio / any FinOps tool that consumes the FinOps "
                "Foundation's open billing schema (ratified Dec 2025). The "
                "rows populate the list-price columns (ListCost, ListUnitPrice, "
                "PricingQuantity, etc.) and leave billed/contracted/effective "
                "columns NULL since cloudprice produces projections, not real "
                "bills — that's explicitly documented in the result's notes "
                "field. Use this when the user says 'export my comparison to "
                "FOCUS', 'send this to Vantage', 'plug this into our FinOps "
                "stack'. Supports query_kind: compute_workload (calls "
                "assess_migration), token_pricing (compare_token_pricing), "
                "extended_model_lookup (lookup_extended_model_pricing). Format "
                "is 'json' (default) or 'csv'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query_kind": {
                        "type": "string",
                        "enum": ["compute_workload", "token_pricing", "extended_model_lookup"],
                        "description": "Which underlying pricing tool to run before FOCUS-shaping.",
                    },
                    "format": {"type": "string", "enum": ["json", "csv"], "default": "json"},
                    "billing_period_start": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD for the synthetic billing period start. Defaults to current month.",
                    },
                    # compute_workload args (use the standard inventory shape via source_cloud)
                    "source_cloud": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                    "compute": {"type": "array"},
                    "storage": {"type": "array"},
                    "object_storage": {"type": "array"},
                    "egress": {"type": "array"},
                    "databases": {"type": "array"},
                    "commitment": {"type": "string"},
                    "multi_az": {"type": "boolean"},
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                    },
                    # token_pricing args
                    "model_family": {
                        "type": "string",
                        "enum": ["claude", "gpt", "gemini", "llama", "mistral", "deepseek"],
                    },
                    "model_id": {"type": "string"},
                    "providers": {"type": "array", "items": {"type": "string"}},
                    "monthly_input_tokens": {"type": "integer", "minimum": 0},
                    "monthly_output_tokens": {"type": "integer", "minimum": 0},
                    # extended_model_lookup args
                    "query": {"type": "string"},
                    "provider": {"type": "string"},
                    "mode": {"type": "string", "enum": ["chat", "completion", "responses"]},
                    "source": {"type": "string", "enum": ["litellm", "openrouter"]},
                },
                "required": ["query_kind"],
            },
        ),
        # --- v0.16.0 / v0.17.0 extended LLM catalog (LiteLLM + OpenRouter) ---
        Tool(
            name="lookup_extended_model_pricing",
            description=(
                "Search the extended LLM catalog (~2300 model/provider combinations "
                "auto-ingested weekly from two upstreams: LiteLLM's public JSON "
                "(direct-provider retail prices for Together AI, Fireworks, Replicate, "
                "Groq, Cerebras, Perplexity, regional Bedrock/Azure variants, etc.) "
                "and OpenRouter's /api/v1/models (routed prices including OpenRouter's "
                "margin). Every row tagged source='litellm' or source='openrouter' "
                "so callers can compare direct vs routed pricing for the same model. "
                "Complements compare_token_pricing (hand-curated, 19 vetted models). "
                "Use this when the user asks about: (a) a model or provider "
                "compare_token_pricing doesn't know — 'cheapest place to host Llama "
                "3.1 405B', 'Together AI pricing', 'what Groq charges for Mixtral' — "
                "or (b) whether OpenRouter routing is cheaper or more expensive than "
                "going direct to Bedrock/Anthropic/OpenAI ('does OpenRouter add margin "
                "for Claude 3 Haiku?')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive substring matched against the model ID. E.g. 'llama-3.1-70b' matches every Llama 3.1 70B variant on every host.",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Filter to one provider: 'bedrock', 'fireworks_ai', 'together_ai', 'groq', 'perplexity', 'replicate', 'openrouter' (for routed prices), etc.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["chat", "completion", "responses"],
                        "description": "Mode filter. Default: any (catalog already excludes embedding/image/audio).",
                    },
                    "max_context_tokens": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Skip models with smaller context. Useful for 'cheapest models that support 200K+ context'.",
                    },
                    "monthly_input_tokens": {"type": "integer", "minimum": 0},
                    "monthly_output_tokens": {"type": "integer", "minimum": 0},
                    "source": {
                        "type": "string",
                        "enum": ["litellm", "openrouter"],
                        "description": "Filter to one upstream source. 'litellm' = direct-provider retail prices. 'openrouter' = OpenRouter routed prices (includes their margin). Default: both — useful for cross-source comparison.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "default": 25},
                },
                "additionalProperties": False,
            },
        ),
        # --- v0.11.0 GPU pricing ---
        Tool(
            name="compare_gpu_workload",
            description=(
                "Cross-cloud GPU pricing comparison — finds the cheapest matching SKU "
                "per cloud for a (gpu_type, gpu_count) request. Supports NVIDIA T4 / "
                "A10 / A10G / L4 / L40S / V100 / A100 / H100 across AWS / Azure / GCP / "
                "OCI. Returns absolute hourly cost AND $/GPU/h so users can see both "
                "the absolute winner and the per-GPU packaging-efficiency winner "
                "(they're often different clouds). Surfaces over-provisioning when "
                "the only matching SKU has more GPUs than requested (e.g., asking for "
                "1 H100 but only bare-metal 8x SKUs are available). Use for AI/ML "
                "workload pricing decisions — training, inference, fine-tuning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "gpu_type": {
                        "type": "string",
                        "description": "GPU family name, e.g. 'A100', 'H100', 'L4', 'L40S', 'T4', 'A10', 'A10G', 'V100'. Case-insensitive.",
                    },
                    "gpu_count": {"type": "integer", "minimum": 1, "default": 1},
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Optional list of clouds to compare; default all 4.",
                    },
                },
                "required": ["gpu_type"],
                "additionalProperties": False,
            },
        ),
        # --- v0.10.0 Carbon-aware FinOps ---
        Tool(
            name="compare_carbon_footprint",
            description=(
                "Multi-cloud carbon footprint comparison — the only FinOps tool that "
                "returns both USD cost AND kg CO2e/month side-by-side. For a given "
                "vCPU + memory shape (and optional quantity), returns per-cloud best-"
                "matching SKU + monthly cost + grid-based kg CO2e + market-based "
                "residual kg CO2e (after the cloud's published renewable matching) + "
                "power class (x86 vs ARM). Ranked cheapest-carbon-first. Surfaces "
                "tradeoffs (AWS/Azure 100% renewable matched, GCP ~64% CFE, OCI "
                "unmatched outside EU). Use when the user cares about sustainability, "
                "Green FinOps, or carbon-cost dual optimization."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vcpus": {"type": "integer", "minimum": 1},
                    "memory_gb": {"type": "number", "minimum": 0.5},
                    "quantity": {"type": "integer", "minimum": 1, "default": 1},
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Optional list of clouds to compare; default all 4.",
                    },
                },
                "required": ["vcpus", "memory_gb"],
                "additionalProperties": False,
            },
        ),
        # --- v0.9.0 Cost Drift Sentinel ---
        Tool(
            name="watch_workload",
            description=(
                "Cost Drift Sentinel — turns cloudprice from query tool into agent capability. "
                "First call (no baseline): captures the current per-cloud monthly cost for the "
                "workload as a baseline JSON the caller persists. Subsequent calls (with the "
                "saved baseline): compute today's cost, compare to baseline, return a structured "
                "drift report with per-cloud delta + SKU-level attribution from the price-history "
                "dataset. Stateless — no database, no server-side storage. Drop the baseline JSON "
                "into a git repo or any durable location. Designed for scheduled agents (GitHub "
                "Actions cron, Claude Code's autonomous mode, etc.) to detect 'has this cost "
                "moved since I signed off?' between runs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_finops_inventory_properties(),
                    "baseline": {
                        "type": ["object", "null"],
                        "description": (
                            "Previously returned baseline JSON. Pass null on first call to "
                            "capture a fresh baseline; pass it back on subsequent calls to "
                            "detect drift."
                        ),
                    },
                    "alert_threshold_pct": {
                        "type": "number",
                        "minimum": 0,
                        "default": 5.0,
                        "description": "Drift % that triggers an alert. Defaults to 5%.",
                    },
                },
                "required": ["source_cloud"],
                "additionalProperties": False,
            },
        ),
        # --- v0.8.1 spot pricing tool ---
        Tool(
            name="compare_spot",
            description=(
                "Compare spot / preemptible pricing for a vCPU + memory shape across "
                "AWS, Azure, GCP, and OCI. Returns per-cloud best-matching SKU + "
                "on-demand cost + spot cost + savings + eviction characteristics, "
                "ranked cheapest-first. The only multi-cloud spot comparison "
                "anyone publishes openly — AWS Spot vs Azure Spot vs GCP Spot VMs "
                "vs OCI Preemptible all use different pricing + eviction models, "
                "and this tool surfaces every one of them with their tradeoffs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vcpus": {"type": "integer", "minimum": 1},
                    "memory_gb": {"type": "number", "minimum": 0.5},
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Optional list of clouds to compare; default all 4.",
                    },
                },
                "required": ["vcpus", "memory_gb"],
                "additionalProperties": False,
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Adapt the protocol-free dispatch in `dispatch.py` to MCP content."""
    # stdio runs on the user's own machine, so local-only tools (reading their
    # own billing export) are allowed here and nowhere else.
    result = dispatch.call(name, arguments, allow_local=True)
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Console entry point. Runs the MCP server over stdio."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
