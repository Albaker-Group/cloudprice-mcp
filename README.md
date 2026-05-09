# cloudprice-mcp

<!-- mcp-name: io.github.alialbaker/cloudprice-mcp -->

[![PyPI version](https://img.shields.io/pypi/v/cloudprice-mcp.svg)](https://pypi.org/project/cloudprice-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/cloudprice-mcp.svg)](https://pypi.org/project/cloudprice-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![alialbaker/cloudprice-mcp MCP server](https://glama.ai/mcp/servers/alialbaker/cloudprice-mcp/badges/score.svg)](https://glama.ai/mcp/servers/alialbaker/cloudprice-mcp)

**The FinOps MCP server.** Gives Claude, GitHub Copilot, Cursor, Windsurf, Cline, Continue, Zed — or any MCP-compatible AI — structured pricing data and analysis primitives across **AWS, Azure, GCP, and OCI**. AI clients use cloudprice-mcp to compute Reserved Instance break-even, multi-cloud workload TCO, exit-cost migration analyses, snapshot cost modeling, and egress arbitrage — the kind of FinOps decisions that normally live in three browser tabs and a half-built spreadsheet.

**10 tools** covering compute, block storage, object storage, managed Postgres, **egress** (internet + inter-region with OCI's 10 TB free tier surfaced explicitly), Multi-AZ workloads, snapshots with realistic incremental modeling, and Reserved Instance / Savings Plan discounts. OCI Always Free tier (4 OCPU compute, 20 GB object storage, 10 TB egress) surfaced as $0 line items where it applies.

**One-line install configures every AI client you have:** `pip install cloudprice-mcp && cloudprice-mcp setup` — auto-detects Claude Desktop, GitHub Copilot Agent Mode, Cursor, Windsurf, Cline, Continue.dev, and Zed, then asks Y/N before writing each config.

![demo](demo.gif)

## What does FinOps look like with cloudprice-mcp?

Real questions teams actually ask. Paste any of these into Claude / Copilot / Cursor with cloudprice-mcp loaded:

> ***"I have 6× t3.2xlarge running on AWS. Compare the 3-year total cost on-demand vs 1-year Savings Plan vs 3-year RI partial upfront. What's the break-even month?"***
> → AI calls `compare_workload`, pulls list-price baseline, layers AWS's published RI rates, returns dollar break-even. ~7-month payback typical.

> ***"I'm thinking about offloading 5 TB of cold-tier object storage from AWS S3 to a cheaper provider. Compare archive-tier cost across all 4 clouds, factor in AWS exit egress, and tell me the payback period."***
> → AI calls `compare_object_storage` + `compare_egress`, computes one-time exit cost vs ongoing savings. Often surfaces "don't move — AWS Glacier Deep Archive is already tied for cheapest".

> ***"At 50 TB/month internet egress, where am I cheapest? Show the 3-year savings of moving."***
> → `compare_egress` → OCI ~$340/mo, AWS/Azure/GCP ~$4,000/mo. The 12× difference is OCI's 10 TB free tier — a real moat for content/CDN workloads.

> ***"Size a 3-tier SaaS workload: 8 web (4/16), 12 app (8/32), 4 DB (16/64), 5 TB shared SSD, 50 TB HDD bulk, 10 TB/month egress. Compare full-stack monthly cost across all 4 clouds with multi-AZ and 1-year commitment."***
> → AI chains `compare_workload` + `compare_egress`, applies multi-AZ multiplier (×2 compute) + commitment discount.

What you get back: dollar numbers traceable to a public catalog, AI-explained tradeoffs, payback periods, and the kind of "don't do that" recommendation that kills bad migrations before they happen. **No console-clicking. No tab-switching between three pricing calculators. No FinOps spreadsheet that goes stale the moment a new SKU drops.**

---

## Install

**Recommended (auto-config):**

```bash
pip install cloudprice-mcp
cloudprice-mcp setup     # auto-configures every detected MCP client, asks Y/N before writing
```

Then fully restart whichever clients were configured. **10 tools appear** in each. Done.

**Trust spectrum:**

| Command | When to use |
|---|---|
| `cloudprice-mcp setup` | Default — detects every installed client, shows the plan, asks Y/N once |
| `cloudprice-mcp setup --yes` | Skip prompt (CI / scripts) |
| `cloudprice-mcp setup --client copilot` | Configure a specific client (repeatable: `--client copilot --client cursor`) |
| `cloudprice-mcp setup --all` | Configure every known client even if not detected |
| `cloudprice-mcp setup --force` | Refresh existing entries — useful after upgrade or moving Python |
| `cloudprice-mcp setup --dry-run` | Show per-client diffs without writing |
| `cloudprice-mcp setup --print-config` | Emit per-client JSON to stdout for manual paste |
| `cloudprice-mcp setup --list-clients` | Detection table — which clients are known + installed on this system |
| Manual edit | Don't trust running new tools — see [INSTALL.md](INSTALL.md) per-client sections |

If something doesn't work, run:

```bash
cloudprice-mcp doctor
```

It tells you exactly what's broken (Python version, install path, config location, tool registration, command path validity).

Python 3.10+ required.

For step-by-step manual install (Windows / macOS / Linux), see **[INSTALL.md](INSTALL.md)**.

## Tools exposed

### Single-spec lookups (v0.1)

| Tool | What it does |
|---|---|
| `get_aws_price` | Look up an EC2 instance type → vCPUs, memory, hourly + monthly USD (us-east-1) |
| `get_azure_price` | Look up an Azure VM size → vCPUs, memory, hourly + monthly USD (eastus) |
| `get_gcp_price` | Look up a GCP Compute Engine machine type → vCPUs, memory, hourly + monthly USD (us-east1) |
| `compare_clouds` | Given a target spec (vCPUs + GB), return the cheapest matching SKU across **AWS / Azure / GCP / OCI**, sorted by monthly cost, with savings summary |

### Bulk + workload compare (v0.2)

| Tool | What it does |
|---|---|
| `compare_compute_inventory` | Bulk-compare a list of compute workloads (each with vCPUs / memory / quantity / hours / optional OS disk) across all 4 clouds. Returns per-row matches, per-cloud totals, cheapest cloud. |
| `compare_storage_inventory` | Bulk-compare a list of block-storage volumes (each with capacity / disk type / quantity) across all 4 clouds. |
| `compare_workload` | Combined compute + block storage in one call. Mirrors a two-sheet sizing workbook (compute BoM + storage BoM). Optional `commitment` overlay applies 1-year (30%) or 3-year (50%) compute discount. |

### Object storage + managed Postgres (v0.3)

| Tool | What it does |
|---|---|
| `compare_object_storage` | Bulk-compare object-storage buckets across **AWS S3 / Azure Blob / GCP Cloud Storage / OCI Object Storage**. Each row specifies capacity_gb + tier (`hot` / `cool` / `archive`). **OCI Always Free 20 GB tier surfaced explicitly** — capacity ≤ 20 GB on OCI hot tier returns $0/mo. |
| `compare_postgres_database` | Bulk-compare managed PostgreSQL pricing across **AWS RDS / Azure Database for PostgreSQL / GCP Cloud SQL / OCI Database with PostgreSQL**. Each row specifies vCPUs / memory / storage_gb. Storage cost is calculated separately from compute. |

### Egress + Multi-AZ + better snapshots (v0.5, NEW)

| Tool / Feature | What it does |
|---|---|
| `compare_egress` | Compare data-transfer costs across all 4 clouds. Two directions: `out_to_internet` (tiered pricing with free-tier credits — AWS/Azure 100 GB, **OCI 10 TB**) and `inter_region` (cross-region within the same cloud). At 50 TB/month internet egress, **OCI is ~12× cheaper than the hyperscalers** — a real moat for content/CDN workloads. |
| `compare_workload` `multi_az: true` | New flag doubles compute totals on every cloud to model Multi-AZ / HA deployments (sync replicas across two zones). Storage stays at 1× because object/block storage is usually cross-AZ at base price. |
| `snapshot_incremental_factor` | New per-row field on storage and OS-disk snapshots. Default `1.0` keeps the v0.2 upper-bound estimate. Set to `0.3` for typical real-world incremental dedup, or `0.0` to exclude snapshots from the total. |

### Example: compare_workload input shape

```json
{
  "compute": [
    { "name": "web", "tier": "Web", "vcpus": 4, "memory_gb": 16, "quantity": 8,  "os_disk_gb": 100, "os_disk_type": "ssd" },
    { "name": "app", "tier": "App", "vcpus": 8, "memory_gb": 32, "quantity": 12, "os_disk_gb": 200, "os_disk_type": "ssd" },
    { "name": "db",  "tier": "DB",  "vcpus": 16, "memory_gb": 64, "quantity": 4, "os_disk_gb": 500, "os_disk_type": "ssd" }
  ],
  "storage": [
    { "name": "shared-fast", "tier": "DB",  "capacity_gb": 5000,  "disk_type": "ssd" },
    { "name": "shared-bulk", "tier": "App", "capacity_gb": 50000, "disk_type": "hdd" }
  ]
}
```

### Snapshots (v0.2.1)

`snapshot_count` on storage rows and `os_disk_snapshot_count` on compute rows **are now priced**. Snapshot rates per cloud per disk type are bundled (~$0.05/GB-mo for AWS/Azure, ~$0.026/GB-mo for GCP).

**Caveat — upper-bound estimate:** snapshots are priced as `snapshot_per_gb_month × full_capacity × quantity × snapshot_count`. Real-world snapshots are **incremental** (only changed blocks), so actual cost is typically 20-50% of this model's number. If snapshots dominate your total, ask the cloud's calculator for a tighter estimate.

`iops` and `throughput_mbs` on storage rows are still accepted as metadata only — not used for SKU matching in this release.

### Reserved Instance / Savings Plan estimator (v0.2.1)

`compare_workload` accepts an optional `commitment` parameter:

| Value | Compute discount | Use case |
|---|---|---|
| `none` (default) | 0% | On-demand only |
| `1yr_no_upfront` | 30% | 1-year AWS Savings Plan / Azure RI / GCP CUD (no upfront) |
| `3yr_partial_upfront` | 50% | 3-year, partial upfront — typical "we know our baseline" deals |

Storage and snapshots are not discounted (most clouds don't offer meaningful storage commitments). Discount tiers are conservative averages — your actual rate depends on instance family, payment option, and region.

## Pricing data

Prices are bundled as a curated dataset of common SKUs across **4 clouds**:
- **Compute** (~50 VM SKUs across AWS / Azure / GCP / OCI, including OCI A1 Always Free + A2 Arm Ampere + E5 Flex)
- **Block storage** (SSD + HDD per cloud)
- **Object storage** (Hot / Cool / Archive tiers per cloud, including OCI Always Free 20 GB)
- **Managed PostgreSQL** (RDS / Azure DB / Cloud SQL / OCI Database with PostgreSQL)

OCI pricing is verified against [Oracle's public pricing API](https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/). Each response includes an `as_of` date so you know how fresh the data is.

### What's NOT modeled (real-world TCO killers)
- ✅ ~~Egress / data transfer~~ — **modeled in v0.5** (`compare_egress`)
- ✅ ~~Multi-AZ / HA replicas~~ — **modeled in v0.5** (`multi_az: true` on `compare_workload`)
- ✅ ~~Snapshots upper-bound only~~ — **fixed in v0.5** (`snapshot_incremental_factor`)
- **Reserved/Savings Plan SKU detail** (we apply a flat tier discount, not per-region/per-family detail) — roadmap
- **Multi-region pricing** (currently us-east only; us-west / eu-west planned for v0.5.1) — roadmap
- **IOPS-based storage matching** (capacity-only) — roadmap
- **Backup storage charges** (some clouds free, others billed) — roadmap
- **Request costs** (PUT/GET pricing for object storage) — roadmap
- **Retrieval costs** for archive tiers (Glacier-style retrieval can be 10× the storage cost) — roadmap
- **VPC peering / interconnect costs** — roadmap

These are tracked roadmap items. **Use cloudprice-mcp for the on-demand list-price baseline; do final TCO analysis with each cloud's own calculator before relying on numbers for big decisions.**

**Live API mode is on the roadmap** (issue #1) — would fetch prices directly from each cloud's public pricing API:
- AWS: [Price List Bulk API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/price-changes.html)
- Azure: [Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
- GCP: [Cloud Billing Catalog API](https://cloud.google.com/billing/docs/reference/rest/v1/services.skus)

Track [issue #1](https://github.com/alialbaker/cloudprice-mcp/issues/1) for live mode and [issue #2](https://github.com/alialbaker/cloudprice-mcp/issues/2) for cross-cloud service mapping (RDS↔SQL DB↔Cloud SQL, etc.).

## Develop locally

```bash
git clone https://github.com/alialbaker/cloudprice-mcp.git
cd cloudprice-mcp
pip install -e ".[dev]"
pytest
```

To point Claude Desktop at your dev copy, swap the `command` in the config:

```json
{
  "mcpServers": {
    "cloudprice": {
      "command": "python",
      "args": ["-m", "cloudprice_mcp.server"]
    }
  }
}
```

## License

MIT — see [LICENSE](LICENSE).

## Credits

Built by [Ali Albaker](https://cloud.albaker.info), multi-cloud architect — runs a live three-cloud portfolio at ~$1.80/month across AWS, Azure, and GCP, with OCI joining as the 4th cloud in 2026.
