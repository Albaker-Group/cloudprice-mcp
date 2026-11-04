"""estimate_agent_cost — what one run of an AI agent costs, and a month of them.

Per-call price math ("$3 per million input tokens") badly underestimates an
agent. Each step re-sends the whole conversation so far — system prompt, tool
definitions, every earlier model output and tool result — so input tokens grow
with every step, and a 10-step run costs far more than ten single calls.
Prompt caching is what brings that back down, and only some providers price it.

Everything here is deterministic arithmetic over the weekly LLM catalogue; the
model never estimates a number itself.

Model of one run with n steps, prefix P (system prompt + tool definitions),
user input U, O output tokens per step and R tool-result tokens per step:

    input at step k  = P + U + (k - 1) * (O + R)
    output at step k = O

With caching, step 1 writes P + U to the cache; every later step reads what the
previous request sent and writes only what is new since (O + R).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

from .extended_tokens import _load_catalog

# Popular current models, compared when the caller names none. Missing IDs
# (renamed or delisted upstream) are skipped, not errors.
DEFAULT_COMPARE = (
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "gpt-5",
    "gpt-5-mini",
    "gemini/gemini-2.5-pro",
    "gemini/gemini-2.5-flash",
    "deepseek/deepseek-chat",
)
_FIRST_PARTY = ("anthropic", "openai", "google", "deepseek", "mistral", "vertex", "bedrock")


@dataclass(frozen=True)
class AgentShape:
    steps: int
    system_prompt_tokens: int
    tool_definition_tokens: int
    user_input_tokens: int
    output_tokens_per_step: int
    tool_result_tokens_per_step: int
    runs_per_month: int

    def inputs(self) -> list[int]:
        base = self.system_prompt_tokens + self.tool_definition_tokens + self.user_input_tokens
        grow = self.output_tokens_per_step + self.tool_result_tokens_per_step
        return [base + k * grow for k in range(self.steps)]


def estimate_agent_cost(
    *,
    model: str,
    steps: int = 8,
    system_prompt_tokens: int = 2_000,
    tool_definition_tokens: int = 3_000,
    user_input_tokens: int = 500,
    output_tokens_per_step: int = 400,
    tool_result_tokens_per_step: int = 1_500,
    runs_per_month: int = 10_000,
    provider: str | None = None,
    compare_models: list[str] | None = None,
    hosting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    shape = AgentShape(
        steps=steps,
        system_prompt_tokens=system_prompt_tokens,
        tool_definition_tokens=tool_definition_tokens,
        user_input_tokens=user_input_tokens,
        output_tokens_per_step=output_tokens_per_step,
        tool_result_tokens_per_step=tool_result_tokens_per_step,
        runs_per_month=runs_per_month,
    )
    _validate(shape)
    catalog = _load_catalog()
    row = _find(catalog["models"], model, provider)
    if row is None:
        raise ValueError(
            f"model '{model}' not found in the LLM catalogue; use "
            "lookup_extended_model_pricing to find its exact ID"
        )

    inputs = shape.inputs()
    run = _price_run(row, shape)
    flat = inputs[0] * steps
    growth_share = 1 - flat / sum(inputs) if sum(inputs) else 0.0

    names = compare_models if compare_models is not None else list(DEFAULT_COMPARE)
    alternatives = []
    for name in names:
        alt = _find(catalog["models"], name, None)
        if alt is None or _key(alt) == _key(row):
            continue
        priced = _price_run(alt, shape)
        alternatives.append(
            {
                "model_id": alt["model_id"],
                "provider": alt["provider"],
                "cost_per_run_usd": priced["best_per_run_usd"],
                "monthly_usd": round(priced["best_per_run_usd"] * runs_per_month, 2),
                "uses_caching": priced["cached"] is not None,
            }
        )
    alternatives.sort(key=lambda a: a["cost_per_run_usd"])

    warnings = []
    window = row.get("context_window_tokens")
    if window and inputs[-1] > window:
        warnings.append(
            f"The last step sends {inputs[-1]:,} input tokens, more than this model's "
            f"{window:,}-token context window: the run would fail or need truncation."
        )

    best = run["best_per_run_usd"]
    hosted = _hosting(hosting, shape, best) if hosting else None
    if hosted:
        warnings.extend(hosted.pop("warnings"))
    return {
        "kind": "agent_cost_estimate",
        "catalog_as_of": catalog["as_of"],
        "model": {
            "model_id": row["model_id"],
            "provider": row["provider"],
            "input_per_1m_usd": row["input_per_1m_usd"],
            "output_per_1m_usd": row["output_per_1m_usd"],
            "cache_read_per_1m_usd": row.get("cache_read_per_1m_usd"),
            "cache_write_per_1m_usd": row.get("cache_write_per_1m_usd"),
        },
        "shape": shape.__dict__,
        "headline": _headline(row, shape, run, growth_share),
        "per_run": {
            "input_tokens": sum(inputs),
            "output_tokens": output_tokens_per_step * steps,
            "uncached_usd": run["uncached"],
            "cached_usd": run["cached"],
            "best_usd": best,
        },
        "monthly_usd": round(best * runs_per_month, 2),
        "context_growth": {
            "first_step_input_tokens": inputs[0],
            "last_step_input_tokens": inputs[-1],
            "share_of_input_from_growth": round(growth_share, 3),
            "explanation": (
                "Each step re-sends the conversation so far. Without that growth "
                f"this run would send {flat:,} input tokens; it sends {sum(inputs):,}."
            ),
        },
        "caching_saves_pct": (
            round((1 - run["cached"] / run["uncached"]) * 100, 1)
            if run["cached"] is not None and run["uncached"]
            else None
        ),
        "steps": [
            {"step": k + 1, "input_tokens": t, "output_tokens": output_tokens_per_step}
            for k, t in enumerate(inputs)
        ],
        "alternatives": alternatives,
        "hosting": hosted,
        "warnings": warnings,
        "honest_gaps": [
            "Token counts are your estimates; measure a real run (most SDKs return "
            "usage per call) and pass those numbers for a reliable figure.",
            "Caching assumes each step reuses the previous request's prefix within the "
            "cache lifetime (minutes) and that the provider caches automatically or you "
            "set cache breakpoints. Step 1 is priced as a cache write every run.",
            "Hosting is only included when `hosting` is passed (AgentCore Runtime, "
            "Gateway and Memory, and Lambda x86, us-east-1 list prices, no free tier). "
            "Not included: containers on ECS/EKS, vector "
            "search, tool API fees, retries, or reasoning/thinking tokens that some "
            "models bill as output.",
            "Prices are list prices from LiteLLM's community-maintained catalogue; "
            "batch, committed-use and enterprise discounts are not applied.",
        ],
    }


def _validate(s: AgentShape) -> None:
    if s.steps < 1:
        raise ValueError("steps must be at least 1")
    for name, value in s.__dict__.items():
        if value < 0:
            raise ValueError(f"{name} cannot be negative")


def _key(row: dict[str, Any]) -> tuple[str, str]:
    return (row["model_id"], row["provider"])


def _find(models: list[dict[str, Any]], model: str, provider: str | None) -> dict[str, Any] | None:
    """Exact model ID from LiteLLM (direct provider prices), first-party host
    preferred. OpenRouter rows are skipped: they carry OpenRouter's margin."""
    rows = [
        m
        for m in models
        if m.get("source") == "litellm"
        and m.get("model_id") == model
        and m.get("input_per_1m_usd") is not None
        and m.get("output_per_1m_usd") is not None
        and (provider is None or m.get("provider") == provider)
    ]
    rows.sort(key=lambda m: (m.get("provider") not in _FIRST_PARTY, m.get("provider", "")))
    return rows[0] if rows else None


def _price_run(row: dict[str, Any], s: AgentShape) -> dict[str, Any]:
    pin = row["input_per_1m_usd"] / 1e6
    pout = row["output_per_1m_usd"] / 1e6
    inputs = s.inputs()
    out = s.output_tokens_per_step * s.steps * pout
    uncached = sum(inputs) * pin + out

    cached = None
    read = row.get("cache_read_per_1m_usd")
    if read is not None:
        pread = read / 1e6
        # No separate write price (OpenAI, Gemini implicit caching): writes bill as input.
        write = row.get("cache_write_per_1m_usd")
        pwrite = write / 1e6 if write is not None else pin
        cost = inputs[0] * pwrite
        for prev, cur in zip(inputs, inputs[1:], strict=False):
            cost += prev * pread + (cur - prev) * pwrite
        cached = cost + out

    best = min(uncached, cached) if cached is not None else uncached
    return {
        "uncached": round(uncached, 6),
        "cached": round(cached, 6) if cached is not None else None,
        "best_per_run_usd": round(best, 6),
    }


def _headline(row: dict[str, Any], s: AgentShape, run: dict[str, Any], growth: float) -> str:
    best = run["best_per_run_usd"]
    line = (
        f"A {s.steps}-step run on {row['model_id']} ({row['provider']}) costs about "
        f"${best:.4f}, so {s.runs_per_month:,} runs a month is about "
        f"${best * s.runs_per_month:,.2f}. Context growth accounts for "
        f"{growth:.0%} of the input tokens."
    )
    if run["cached"] is not None and run["uncached"]:
        line += f" Prompt caching cuts the run cost by {1 - run['cached'] / run['uncached']:.0%}."
    return line


def _hosting_prices() -> dict[str, Any]:
    raw = resources.files("cloudprice_mcp.data").joinpath("agent_hosting_prices.json")
    return json.loads(raw.read_text(encoding="utf-8"))


def _hosting(h: dict[str, Any], s: AgentShape, token_usd: float) -> dict[str, Any]:
    """Price the same run on AgentCore Runtime and on Lambda, plus Gateway and
    Memory, and add the token cost for a full per-run and monthly figure.

    The difference that matters: an agent spends most of a run waiting on the
    model. AgentCore bills CPU only while active (memory for the whole run);
    Lambda bills memory-seconds for the whole run, waiting included.
    """
    seconds = float(h.get("run_seconds", 0))
    if seconds <= 0:
        raise ValueError("hosting.run_seconds (wall-clock seconds per run) is required")
    vcpu = float(h.get("vcpu", 1))
    memory_gb = float(h.get("memory_gb", 2))
    active = float(h.get("active_cpu_share", 0.2))
    if not 0 < active <= 1 or vcpu <= 0 or memory_gb <= 0:
        raise ValueError("hosting: vcpu and memory_gb must be positive, active_cpu_share in (0, 1]")
    tool_calls = int(h.get("tool_calls_per_run", s.steps - 1))
    retrievals = int(h.get("memory_retrievals_per_run", 0))
    events = int(h.get("memory_events_per_run", 0))

    p = _hosting_prices()
    rt, gw, mem, lam = (
        p["agentcore_runtime"],
        p["agentcore_gateway"],
        p["agentcore_memory"],
        p["lambda"],
    )
    hours = seconds / 3600
    runtime = (
        vcpu * hours * active * rt["vcpu_hour"] + max(memory_gb, 0.125) * hours * rt["gb_hour"]
    )
    gateway = tool_calls * gw["per_1k_invocations"] / 1000
    memory = (
        retrievals * mem["long_term_per_1k_retrievals"] / 1000
        + events * mem["short_term_event_billed_kb_min"] / 1e6 * mem["short_term_per_gb_ingested"]
    )
    agentcore = runtime + gateway + memory
    lambda_run = memory_gb * seconds * lam["x86_gb_second"] + lam["per_1m_requests"] / 1e6

    def total(hosting_usd: float) -> dict[str, float]:
        per_run = token_usd + hosting_usd
        return {
            "hosting_per_run_usd": round(hosting_usd, 6),
            "total_per_run_usd": round(per_run, 6),
            "total_monthly_usd": round(per_run * s.runs_per_month, 2),
            "hosting_share_of_total": round(hosting_usd / per_run, 3) if per_run else 0.0,
        }

    warnings = []
    if seconds > 900:
        warnings.append(
            f"A {seconds:.0f}-second run exceeds Lambda's 15-minute limit; Lambda "
            "cannot host it as one invocation."
        )
    return {
        "prices_as_of": p["as_of"],
        "assumptions": {
            "run_seconds": seconds,
            "vcpu": vcpu,
            "memory_gb": memory_gb,
            "active_cpu_share": active,
            "tool_calls_per_run": tool_calls,
            "memory_retrievals_per_run": retrievals,
            "memory_events_per_run": events,
        },
        "agentcore": {
            "runtime_usd": round(runtime, 6),
            "gateway_usd": round(gateway, 6),
            "memory_usd": round(memory, 6),
            **total(agentcore),
        },
        "lambda": {
            "note": "Compute only; a Gateway or memory store would be extra.",
            **total(lambda_run),
        },
        # Compute against compute: Lambda here has no Gateway or memory store.
        "cheaper_compute": "agentcore_runtime" if runtime < lambda_run else "lambda",
        "explanation": (
            f"Compute only: AgentCore Runtime ${runtime:.6f} vs Lambda "
            f"${lambda_run:.6f} per run. AgentCore bills CPU for {active:.0%} of the "
            "run (the rest is waiting on the model or tools) and memory for all of it; "
            f"Lambda bills {memory_gb:g} GB for every one of the {seconds:g} seconds. "
            f"Hosting is {total(agentcore)['hosting_share_of_total']:.0%} of the total "
            "on AgentCore: the model tokens dominate."
        ),
        "sources": p["sources"],
        "warnings": warnings,
    }
