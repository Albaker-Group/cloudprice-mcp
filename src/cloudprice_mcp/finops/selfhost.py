"""compare_llm_vs_selfhost — pay per token, or rent GPUs and serve the model yourself?

Joins two datasets nobody else joins: LLM API prices (the weekly catalogue)
and GPU instance prices across AWS, Azure, GCP and OCI.

The self-host side hinges on throughput — how many tokens per second one
server actually generates for this model — and that varies 10x with the
model, quantisation, batch size and serving stack. So the caller supplies it
(measured, e.g. with a vLLM benchmark); this module never guesses it.

    load (token-equivalents) = output + input / prefill_speedup
    capacity per server      = tokens_per_second * 3600 * 730 * utilisation
    servers                  = ceil(load / capacity), at least 1
    self-host monthly        = servers * server hourly price * 730

Input tokens are processed in a parallel prefill pass, far faster than
generation, hence `prefill_speedup`.
"""

from __future__ import annotations

import math
from typing import Any

from ..pricing import HOURS_PER_MONTH, Cloud, PriceCatalog
from .agent_cost import _find
from .extended_tokens import _load_catalog
from .gpu import compare_gpu_workload


def compare_llm_vs_selfhost(
    catalog: PriceCatalog,
    *,
    model: str,
    monthly_input_tokens: int,
    monthly_output_tokens: int,
    gpu_type: str,
    output_tokens_per_second: float,
    gpu_count: int = 1,
    utilization: float = 0.6,
    prefill_speedup: float = 10.0,
    api_provider: str | None = None,
    targets: list[Cloud] | None = None,
) -> dict[str, Any]:
    if monthly_input_tokens < 0 or monthly_output_tokens < 0:
        raise ValueError("token volumes cannot be negative")
    if monthly_input_tokens + monthly_output_tokens == 0:
        raise ValueError("give a monthly token volume")
    if output_tokens_per_second <= 0:
        raise ValueError("output_tokens_per_second must be positive (measure it)")
    if not 0 < utilization <= 1:
        raise ValueError("utilization must be in (0, 1]")
    if prefill_speedup < 1:
        raise ValueError("prefill_speedup must be at least 1")

    llm = _load_catalog()
    row = _find(llm["models"], model, api_provider)
    if row is None:
        raise ValueError(
            f"model '{model}' not found in the LLM catalogue; use "
            "lookup_extended_model_pricing to find its exact ID"
        )
    api_monthly = (
        monthly_input_tokens * row["input_per_1m_usd"]
        + monthly_output_tokens * row["output_per_1m_usd"]
    ) / 1e6

    gpus = compare_gpu_workload(catalog, gpu_type, gpu_count, targets)
    load = monthly_output_tokens + monthly_input_tokens / prefill_speedup
    capacity = output_tokens_per_second * 3600 * HOURS_PER_MONTH * utilization

    options = []
    for g in gpus["per_cloud"]:
        servers = max(1, math.ceil(load / capacity))
        monthly = servers * g["hourly_usd"] * HOURS_PER_MONTH
        options.append(
            {
                "cloud": g["cloud"],
                "sku": g["sku"],
                "gpus_per_server": g["gpu_count_in_sku"],
                "servers": servers,
                "server_hourly_usd": g["hourly_usd"],
                "monthly_usd": round(monthly, 2),
                "load_share_of_capacity": round(load / (servers * capacity), 3),
                "over_provisioned": g["over_provisioned"],
            }
        )
    options.sort(key=lambda o: o["monthly_usd"])
    if not options:
        raise ValueError(f"no cloud in the catalogue rents {gpu_count}x {gpu_type}")

    best = options[0]
    one_server = best["server_hourly_usd"] * HOURS_PER_MONTH
    # Cost per token-equivalent: the API's is fixed; a server's is lowest when
    # full. If a full server still costs more per token than the API, adding
    # volume never helps - self-hosting never breaks even on this GPU.
    api_per_unit = api_monthly / load if load else 0.0
    server_per_unit = one_server / capacity
    # When reachable, the API bill hits one server's cost before that server is full.
    scale = one_server / api_monthly if api_per_unit > server_per_unit else math.inf
    cheaper = "self_host" if best["monthly_usd"] < api_monthly else "api"
    saving = abs(api_monthly - best["monthly_usd"])

    return {
        "kind": "llm_vs_selfhost",
        "catalog_as_of": {"llm": llm["as_of"], "gpu": catalog.as_of},
        "headline": _headline(row, api_monthly, best, cheaper, saving, scale),
        "api": {
            "model_id": row["model_id"],
            "provider": row["provider"],
            "input_per_1m_usd": row["input_per_1m_usd"],
            "output_per_1m_usd": row["output_per_1m_usd"],
            "monthly_usd": round(api_monthly, 2),
        },
        "self_host": options,
        "cheaper": cheaper,
        "monthly_saving_usd": round(saving, 2),
        "break_even": {
            "reachable": math.isfinite(scale),
            "volume_multiple_of_today": round(scale, 2) if math.isfinite(scale) else None,
            "monthly_input_tokens": _scaled(monthly_input_tokens, scale),
            "monthly_output_tokens": _scaled(monthly_output_tokens, scale),
            "api_usd_per_1m_token_equivalents": round(api_per_unit * 1e6, 4),
            "full_server_usd_per_1m_token_equivalents": round(server_per_unit * 1e6, 4),
            "explanation": (
                f"One {best['sku']} server on {best['cloud']} costs "
                f"${one_server:,.0f}/month. The API bill reaches that at about "
                f"{scale:,.1f}x today's volume, while one server still has capacity."
                if math.isfinite(scale)
                else f"Even fully loaded, {best['sku']} costs "
                f"${server_per_unit * 1e6:,.2f} per million tokens against the API's "
                f"${api_per_unit * 1e6:,.2f}, so self-hosting never breaks even at "
                "this throughput. Higher throughput (batching, quantisation) or a "
                "cheaper GPU is what would change that."
            ),
        },
        "assumptions": {
            "output_tokens_per_second": output_tokens_per_second,
            "utilization": utilization,
            "prefill_speedup": prefill_speedup,
            "hours_per_month": HOURS_PER_MONTH,
        },
        "honest_gaps": [
            "Throughput is the input that decides this answer. Measure it for this "
            "model, quantisation and batch size on the GPU you would rent.",
            "Self-host cost is GPU rental only, always on. Not included: engineering "
            "and on-call time, redundancy (a second server for availability), "
            "storage, egress, load balancers, autoscaling to zero, or committed-use "
            "discounts on the GPUs.",
            "The self-hosted model must be an open-weights model you can run; a "
            "closed API model's price is only a benchmark for what quality costs.",
            "API prices are list prices; batch APIs (often 50% off) and caching are "
            "not applied here — see estimate_agent_cost for caching.",
        ],
    }


def _scaled(tokens: int, scale: float) -> int | None:
    return round(tokens * scale) if math.isfinite(scale) else None


def _headline(row, api_monthly, best, cheaper, saving, scale) -> str:
    line = (
        f"API ({row['model_id']} on {row['provider']}): ${api_monthly:,.2f}/month. "
        f"Cheapest self-host: {best['servers']}x {best['sku']} on {best['cloud']} at "
        f"${best['monthly_usd']:,.2f}/month. "
    )
    if cheaper == "api":
        line += f"The API is cheaper by ${saving:,.2f}/month"
        if math.isfinite(scale):
            line += f"; self-hosting breaks even near {scale:,.1f}x this volume."
        else:
            line += "; at this throughput self-hosting never breaks even."
    else:
        line += f"Self-hosting is cheaper by ${saving:,.2f}/month."
    return line
