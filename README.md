# cloudprice-mcp

An MCP server that lets Claude (or any MCP-compatible client) compare on-demand VM pricing across **AWS, Azure, and GCP** in real time.

Ask things like:

> *"How much does a 4 vCPU / 16 GB Linux VM cost across AWS, Azure, and GCP in us-east?"*

> *"What does an EC2 `t3.xlarge` cost per month?"*

> *"Compare a `D4s_v5` against the equivalent on AWS and GCP."*

Claude calls the right tool, you get a clean answer with hourly + monthly cost. No console-clicking. No tab-switching between three pricing calculators.

---

## Install

```bash
pip install cloudprice-mcp
```

Or run without installing:

```bash
pipx run cloudprice-mcp
```

Python 3.10+ required.

## Wire it into Claude Desktop

Edit your Claude Desktop config:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add:

```json
{
  "mcpServers": {
    "cloudprice": {
      "command": "cloudprice-mcp"
    }
  }
}
```

Restart Claude Desktop. The four tools below will show up as available.

## Tools exposed

| Tool | What it does |
|---|---|
| `get_aws_price` | Look up an EC2 instance type → vCPUs, memory, hourly + monthly USD (us-east-1) |
| `get_azure_price` | Look up an Azure VM size → vCPUs, memory, hourly + monthly USD (eastus) |
| `get_gcp_price` | Look up a GCP Compute Engine machine type → vCPUs, memory, hourly + monthly USD (us-east1) |
| `compare_clouds` | Given a target spec (vCPUs + GB), return the cheapest matching SKU on each cloud, sorted by monthly cost, with savings summary |

## Pricing data

Prices are bundled as a curated dataset of the most common VM SKUs per cloud, sourced from the public AWS / Azure / GCP price lists. Each response includes an `as_of` date so you know how fresh the data is.

**v0.2 will add a live mode** that fetches prices directly from each cloud's public pricing API:
- AWS: [Price List Bulk API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/price-changes.html)
- Azure: [Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
- GCP: [Cloud Billing Catalog API](https://cloud.google.com/billing/docs/reference/rest/v1/services.skus)

Track [issue #1](https://github.com/alialbaker/cloudprice-mcp/issues/1) for live mode.

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

Built by [Ali Albaker](https://albaker.info), Cloud Architect — runs a live three-cloud portfolio at ~$1.80/month across AWS, Azure, and GCP.
