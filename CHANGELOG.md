# Changelog

What changed for you in each release. Upgrade with `uvx cloudprice-mcp@latest`
(or `pip install -U cloudprice-mcp`) and restart your MCP client. Releases older
than 0.20.0 are listed on [GitHub Releases](https://github.com/Albaker-Group/cloudprice-mcp/releases).

## 0.27.0 (2026-11-04)

- **New: `review_agentcore`**, a Well-Architected review of an Amazon Bedrock
  AgentCore deployment, run on `terraform show -json` output on your machine.
  It checks runtimes, harnesses and gateways for public network access,
  unauthenticated or debug gateways, permission to call any model, secrets in
  environment variables, idle-session cost, missing harness limits, log retention,
  alarms and cost tags. Each finding gives the resource, the Terraform fix and the
  AWS page behind the rule. Add your FOCUS bill and it also says what the agent
  costs by component and what prompt caching or list-price fixes would save.
  See [Use it on your own bill and deployment](README.md#use-it-on-your-own-bill-and-deployment).

## 0.26.0 (2026-11-04)

- **New: `analyze_ai_spend`**: your AI spend from your bill, by model and by token
  type (input, output, cache read, cache write); what you paid against list price;
  the same model's cost on other providers; how much prompt caching would save;
  and which of your models changed price. Works on AWS Bedrock, Azure OpenAI and
  AI Foundry, Vertex AI and OCI Generative AI lines.

## 0.25.0 (2026-11-04)

- **New: `reprice_bill`**: takes the compute on your bill (vCPU and memory you
  actually used) and prices it like-for-like on AWS, Azure, GCP and OCI, next to
  what you paid.

## 0.24.0 (2026-11-04)

- **New: `forecast_spend`** (month-end and the next months from your bill's
  trend, each with a range, and the fastest-growing services) and
  **`calculate_unit_cost`** (cost per customer, request or order, by month, and
  whether spend grows faster than the business).

## 0.23.0 (2026-11-04)

- **New: `find_commitment_gaps`** (steady on-demand spend that Savings Plans,
  reservations or CUDs could cover, and idle commitments),
  **`detect_bill_anomalies`** (spikes, drops and new spend) and
  **`analyze_cost_allocation`** (spend missing the tags you charge back on).

## 0.22.0 (2026-11-04)

- **New: `analyze_bill`**: point it at a FOCUS export from any cloud and it says
  where the money goes, what moved and how much is covered by commitments. The
  file is read on your machine and never uploaded.

## 0.20.0 (2026-11-04)

- **New: `compare_llm_vs_selfhost`**: API token prices against renting GPUs and
  serving the model yourself, with the volume where self-hosting starts to pay.
