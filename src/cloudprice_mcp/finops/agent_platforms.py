"""compare_agent_platforms — where to host an agent, and what a month of it costs.

Prices the same agent on the managed agent platforms of three clouds and on
plain serverless compute beside them:

  aws    AgentCore Runtime (with Gateway and Memory), Lambda
  azure  Foundry Agent Service (prompt agents and hosted agents), Container Apps
  gcp    Vertex AI Agent Engine (now called Agent Runtime)

An agent spends most of a run waiting on the model, and that is where the
platforms differ: AgentCore bills CPU only while the agent is busy, the others
bill the whole run. Model tokens are priced on each cloud's own model service
from the LLM catalogue, so a model a cloud does not offer shows as unavailable.

Deterministic arithmetic over bundled prices; nothing is called over the network.
"""

from __future__ import annotations

from typing import Any

from .agent_cost import AgentShape, _hosting_prices
from .bill_ai import _PROVIDERS_OF, _base_row, _index, _plain, model_key
from .extended_tokens import _load_catalog

PLATFORMS = (
    ("agentcore", "aws", "AgentCore Runtime"),
    ("lambda", "aws", "Lambda"),
    ("foundry_prompt_agent", "azure", "Foundry prompt agent"),
    ("foundry_hosted_agent", "azure", "Foundry hosted agent"),
    ("container_apps", "azure", "Container Apps"),
    ("vertex_agent_engine", "gcp", "Vertex AI Agent Engine"),
)
# estimate_agent_cost's default run: 8 steps, 2k system prompt, 3k of tools.
_DEFAULT_RUN = AgentShape(8, 2_000, 3_000, 500, 400, 1_500, 1)


def _validate(runs: int, seconds: float, active: float, vcpu: float, memory_gb: float) -> None:
    if runs < 1:
        raise ValueError("runs_per_month must be at least 1")
    if not 0 < seconds <= 28_800:
        raise ValueError("run_seconds must be between 0 and 28800 (8 hours)")
    if not 0 < active <= 1:
        raise ValueError("active_cpu_share must be in (0, 1]")
    if vcpu <= 0 or memory_gb <= 0:
        raise ValueError("vcpu and memory_gb must be positive")


def _model_rows(model: str, models: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """The model's list-price row on each cloud's own model service, or None."""
    index = _index(models)
    key = model_key(model)
    rows = index.get(key) or index.get(_plain(key), [])
    out: dict[str, dict[str, Any] | None] = {}
    for cloud in ("aws", "azure", "gcp"):
        native = [r for r in rows if r["provider"] in _PROVIDERS_OF[cloud]]
        out[cloud] = _base_row(native) if native else None
    return out


def compare_agent_platforms(
    runs_per_month: int = 10_000,
    run_seconds: float = 30.0,
    active_cpu_share: float = 0.2,
    vcpu: float = 1.0,
    memory_gb: float = 2.0,
    tool_calls_per_run: int = 5,
    memory_retrievals_per_run: int = 0,
    model: str | None = None,
    input_tokens_per_run: int | None = None,
    output_tokens_per_run: int | None = None,
    *,
    models: list[dict[str, Any]] | None = None,
    prices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`models` and `prices` default to the bundled LLM catalogue and hosting prices."""
    _validate(runs_per_month, run_seconds, active_cpu_share, vcpu, memory_gb)
    if tool_calls_per_run < 0 or memory_retrievals_per_run < 0:
        raise ValueError("tool_calls_per_run and memory_retrievals_per_run cannot be negative")
    p = prices or _hosting_prices()
    hours, gb = run_seconds / 3600, max(memory_gb, 0.125)
    retrievals = memory_retrievals_per_run / 1000

    rt = p["agentcore_runtime_v1"]
    cpu_per_active = vcpu * hours * rt["vcpu_hour"]  # AgentCore CPU if busy the whole run
    fh, aca, ae, lam = (
        p["foundry_hosted_agent"],
        p["azure_container_apps"],
        p["vertex_agent_engine"],
        p["lambda"],
    )
    managed_memory = retrievals * p["agentcore_memory"]["long_term_per_1k_retrievals"]
    # platform -> (hosting, tools, memory or None when no price is bundled)
    costs: dict[str, tuple[float, float, float | None]] = {
        "agentcore": (
            cpu_per_active * active_cpu_share + gb * hours * rt["gb_hour"],
            tool_calls_per_run * p["agentcore_gateway"]["per_1k_invocations"] / 1000,
            managed_memory,
        ),
        "lambda": (
            memory_gb * run_seconds * lam["x86_gb_second"] + lam["per_1m_requests"] / 1e6,
            0.0,
            None if retrievals else 0.0,
        ),
        "foundry_prompt_agent": (0.0, 0.0, None if retrievals else 0.0),
        "foundry_hosted_agent": (
            vcpu * hours * fh["vcpu_hour"] + memory_gb * hours * fh["gb_hour"],
            0.0,
            None if retrievals else 0.0,
        ),
        "container_apps": (
            vcpu * run_seconds * aca["vcpu_second_active"]
            + memory_gb * run_seconds * aca["gb_second"]
            + aca["per_1m_requests"] / 1e6,
            0.0,
            None if retrievals else 0.0,
        ),
        "vertex_agent_engine": (
            vcpu * hours * ae["vcpu_hour"] + memory_gb * hours * ae["gb_hour"],
            0.0,
            retrievals * ae["memory_bank_per_1k_retrievals"],
        ),
    }

    tokens_in = tokens_out = 0
    rows: dict[str, dict[str, Any] | None] = {}
    if model:
        tokens_in = (
            input_tokens_per_run if input_tokens_per_run is not None else sum(_DEFAULT_RUN.inputs())
        )
        tokens_out = (
            output_tokens_per_run
            if output_tokens_per_run is not None
            else _DEFAULT_RUN.output_tokens_per_step * _DEFAULT_RUN.steps
        )
        if models is None:
            models = [m for m in _load_catalog()["models"] if m.get("source") == "litellm"]
        rows = _model_rows(model, models)

    platforms = []
    for key, cloud, label in PLATFORMS:
        hosting, tools, memory = costs[key]
        row = rows.get(cloud) if model else None
        tokens = (
            (tokens_in * row["input_per_1m_usd"] + tokens_out * row["output_per_1m_usd"]) / 1e6
            if row
            else None
        )
        per_run = hosting + tools + (memory or 0) + (tokens or 0)
        platforms.append(
            {
                "platform": key,
                "cloud": cloud,
                "label": label,
                "per_run_usd": {
                    "hosting": round(hosting, 6),
                    "tools": round(tools, 6),
                    "memory": None if memory is None else round(memory, 6),
                    "model_tokens": None if tokens is None else round(tokens, 6),
                    "total": round(per_run, 6),
                },
                "monthly_usd": round(per_run * runs_per_month, 2),
                "model_id": row["model_id"] if row else None,
                "model_available": None if not model else row is not None,
            }
        )
    ranked = sorted(
        platforms, key=lambda x: (x["model_available"] is False, x["monthly_usd"], x["platform"])
    )

    result = {
        "kind": "agent_platform_comparison",
        "prices_as_of": {
            "aws": p["as_of"],
            "azure": p["foundry_hosted_agent"]["as_of"],
            "gcp": p["vertex_agent_engine"]["as_of"],
        },
        "shape": {
            "runs_per_month": runs_per_month,
            "run_seconds": run_seconds,
            "active_cpu_share": active_cpu_share,
            "vcpu": vcpu,
            "memory_gb": memory_gb,
            "tool_calls_per_run": tool_calls_per_run,
            "memory_retrievals_per_run": memory_retrievals_per_run,
            "model": model,
            "input_tokens_per_run": tokens_in if model else None,
            "output_tokens_per_run": tokens_out if model else None,
        },
        "platforms": ranked,
        "cheapest": ranked[0]["platform"],
        "agentcore_break_even": _break_even(
            platforms,
            next(x for x in platforms if x["platform"] == "agentcore")["per_run_usd"]["total"]
            - cpu_per_active * active_cpu_share,
            cpu_per_active,
        ),
        "sources": p["sources"],
        "honest_gaps": [
            "List prices in us-east-1 / East US / Google's published rate. Free tiers "
            "(Lambda, Container Apps, Agent Engine) and commitment discounts are not applied.",
            "AgentCore uses Runtime v1 rates, the default for a new runtime; v2 starts "
            "faster and costs more per hour.",
            "Only AgentCore is priced with CPU billed while busy. Foundry hosted agents and "
            "Agent Engine are priced for vCPU and memory over the whole run, since their "
            "price pages do not say waiting is free; Container Apps counts a replica "
            "serving a request as active.",
            "Foundry prompt agents have no hosting charge but run no code of your own; "
            "their built-in tools (code interpreter, file search, web search) and Foundry "
            "memory are billed separately and not included.",
            "Lambda and Container Apps have no managed memory or tool gateway: memory is "
            "shown as unpriced when retrievals are asked for, and tool calls are free "
            "because they run in your own code.",
            "Model tokens are list prices on each cloud's own model service, without "
            "caching or batch discounts. A cloud that does not offer the model is marked "
            "unavailable; calling it across clouds is possible but not priced.",
            "Cost only. Security, networking, data residency, team skills and lock-in "
            "are not weighed. Cloud Run is not included yet: its price is not verified.",
        ],
    }
    result["headline"] = _headline(result)
    return result


def _break_even(
    platforms: list[dict[str, Any]], fixed: float, cpu_per_active: float
) -> list[dict[str, Any]]:
    """The busy share of a run below which AgentCore costs less than each other platform.

    Only AgentCore's cost moves with the busy share (fixed + cpu_per_active * share),
    so it crosses each other platform at most once.
    """
    agentcore = next(x for x in platforms if x["platform"] == "agentcore")
    if agentcore["model_available"] is False:
        return []
    out = []
    for x in platforms:
        if x["platform"] == "agentcore" or x["model_available"] is False:
            continue
        share = (x["per_run_usd"]["total"] - fixed) / cpu_per_active
        out.append(
            {
                "versus": x["platform"],
                "agentcore_cheaper_below_active_share": round(share, 3) if 0 < share < 1 else None,
                "agentcore_always_cheaper": share >= 1,
            }
        )
    return out


def _headline(r: dict[str, Any]) -> str:
    runs = r["shape"]["runs_per_month"]
    ranked = [x for x in r["platforms"] if x["model_available"] is not False]
    best, worst = ranked[0], ranked[-1]
    line = (
        f"For {runs:,} runs a month, {best['label']} is cheapest at "
        f"{best['monthly_usd']:,.2f} and {worst['label']} dearest at {worst['monthly_usd']:,.2f}."
    )
    shares = [
        x["per_run_usd"]["model_tokens"] / x["per_run_usd"]["total"]
        for x in ranked
        if x["per_run_usd"]["model_tokens"] and x["per_run_usd"]["total"]
    ]
    if shares:
        line += (
            f" Model tokens are at least {min(shares):.1%} of the cost everywhere, so the "
            "model's price matters far more than where the agent runs."
        )
    if best["platform"] == "foundry_prompt_agent":
        line += " Foundry prompt agents cost nothing to host but run no code of your own."
    runs_code = [x for x in ranked if x["platform"] != "foundry_prompt_agent"]
    if runs_code:
        host = min(runs_code, key=lambda x: x["per_run_usd"]["total"])
        fee = host["per_run_usd"]["total"] - (host["per_run_usd"]["model_tokens"] or 0)
        line += (
            f" To run your own code, {host['label']} hosts it cheapest ({fee * runs:,.2f} a month)."
        )
    lam = next((b for b in r["agentcore_break_even"] if b["versus"] == "lambda"), None)
    if lam and lam["agentcore_cheaper_below_active_share"] is not None:
        line += (
            " AgentCore costs less than Lambda while the agent is busy less than "
            f"{lam['agentcore_cheaper_below_active_share']:.0%} of each run."
        )
    missing = [x["label"] for x in r["platforms"] if x["model_available"] is False]
    if missing:
        line += f" The model is not offered on: {', '.join(missing)}."
    return line
