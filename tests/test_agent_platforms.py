"""compare_agent_platforms: one agent priced on six hosts.

Fixed prices and models, so the bundled sheets and the weekly refresh cannot
move any figure. The shape is 10,000 runs of 36 s (0.01 h), busy 25% of the time,
1 vCPU, 2 GB, 4 tool calls.
"""

from __future__ import annotations

import pytest

from cloudprice_mcp import dispatch
from cloudprice_mcp.finops.agent_platforms import compare_agent_platforms

PRICES = {
    "as_of": "2026-11-04",
    "sources": {},
    "agentcore_runtime_v1": {"vcpu_hour": 0.09, "gb_hour": 0.01},
    "agentcore_gateway": {"per_1k_invocations": 0.005},
    "agentcore_memory": {"long_term_per_1k_retrievals": 0.5},
    "lambda": {"x86_gb_second": 0.00001, "per_1m_requests": 0.2},
    "foundry_hosted_agent": {"vcpu_hour": 0.1, "gb_hour": 0.012, "as_of": "2026-11-05"},
    "foundry_prompt_agent": {},
    "azure_container_apps": {
        "vcpu_second_active": 0.000024,
        "gb_second": 0.000003,
        "per_1m_requests": 0.4,
        "as_of": "2026-11-05",
    },
    "vertex_agent_engine": {
        "vcpu_hour": 0.0864,
        "gb_hour": 0.009,
        "memory_bank_per_1k_retrievals": 0.5,
        "as_of": "2025-12-16",
    },
}


def m(model_id, provider, inp, out):
    return {
        "litellm_id": model_id,
        "provider": provider,
        "mode": "chat",
        "input_per_1m_usd": inp,
        "output_per_1m_usd": out,
    }


MODELS = [
    m("anthropic.claude-sonnet-5-5", "bedrock", 2.0, 10.0),
    m("azure_ai/claude-sonnet-5-5", "azure_ai", 2.0, 10.0),
    m("vertex_ai/claude-sonnet-5-5", "vertex", 2.2, 11.0),
    m("gpt-5", "openai", 1.25, 10.0),  # the maker's API is no cloud's own service
    m("azure/gpt-5", "azure_openai", 1.25, 10.0),
]


def run(**kw):
    shape = {
        "runs_per_month": 10_000,
        "run_seconds": 36,
        "active_cpu_share": 0.25,
        "tool_calls_per_run": 4,
    }
    return compare_agent_platforms(**{**shape, **kw}, models=MODELS, prices=PRICES)


def monthly(r):
    return [(x["platform"], x["monthly_usd"]) for x in r["platforms"]]


def test_hosting_only_ranking_and_break_even():
    r = run()
    assert monthly(r) == [
        ("foundry_prompt_agent", 0.0),
        ("agentcore", 4.45),  # CPU 0.01 h x 0.09 x 25% + 2 GB x 0.01 h x 0.01 + 4 x 0.000005
        ("lambda", 7.2),  # 2 GB x 36 s x 0.00001 + one request
        ("vertex_agent_engine", 10.44),  # 0.01 h x (0.0864 + 2 x 0.009)
        ("container_apps", 10.8),  # 36 s x (0.000024 + 2 x 0.000003) + one request
        ("foundry_hosted_agent", 12.4),  # 0.01 h x (0.1 + 2 x 0.012)
    ]
    agentcore = r["platforms"][1]["per_run_usd"]
    assert agentcore == {
        "hosting": 0.000425,
        "tools": 0.00002,
        "memory": 0.0,
        "model_tokens": None,
        "total": 0.000445,
    }
    shares = {b["versus"]: b for b in r["agentcore_break_even"]}
    # fixed 0.00022 + 0.0009 x share = Lambda's 0.0007202 -> share 0.556
    assert shares["lambda"]["agentcore_cheaper_below_active_share"] == 0.556
    assert shares["vertex_agent_engine"]["agentcore_cheaper_below_active_share"] == 0.916
    assert shares["foundry_hosted_agent"]["agentcore_always_cheaper"] is True
    assert shares["foundry_prompt_agent"] == {
        "versus": "foundry_prompt_agent",
        "agentcore_cheaper_below_active_share": None,
        "agentcore_always_cheaper": False,
    }
    assert r["headline"] == (
        "For 10,000 runs a month, Foundry prompt agent is cheapest at 0.00 and Foundry "
        "hosted agent dearest at 12.40. Foundry prompt agents cost nothing to host but run "
        "no code of your own. To run your own code, AgentCore Runtime hosts it cheapest "
        "(4.45 a month). AgentCore costs less than Lambda while the agent is busy less "
        "than 56% of each run."
    )


def test_model_tokens_priced_on_each_clouds_own_service():
    r = run(model="claude-sonnet-5-5", input_tokens_per_run=100_000, output_tokens_per_run=2_000)
    assert monthly(r) == [
        ("foundry_prompt_agent", 2200.0),  # 0.1 x 2.00 + 0.002 x 10.00 = 0.22 a run
        ("agentcore", 2204.45),
        ("lambda", 2207.2),
        ("container_apps", 2210.8),
        ("foundry_hosted_agent", 2212.4),
        ("vertex_agent_engine", 2430.44),  # Vertex lists the model 10% higher
    ]
    ids = {x["platform"]: x["model_id"] for x in r["platforms"]}
    assert ids["agentcore"] == "anthropic.claude-sonnet-5-5"
    assert ids["vertex_agent_engine"] == "vertex_ai/claude-sonnet-5-5"
    assert "Model tokens are at least 99.4% of the cost everywhere" in r["headline"]


def test_a_model_a_cloud_does_not_offer():
    r = run(model="gpt-5", input_tokens_per_run=100_000, output_tokens_per_run=2_000)
    available = {x["platform"]: x["model_available"] for x in r["platforms"]}
    assert available["foundry_prompt_agent"] is True
    assert available["agentcore"] is False and available["vertex_agent_engine"] is False
    assert r["platforms"][-1]["model_available"] is False  # ranked after the rest
    assert r["agentcore_break_even"] == []
    assert r["headline"].endswith(
        "The model is not offered on: AgentCore Runtime, Lambda, Vertex AI Agent Engine."
    )


def test_default_token_run_and_memory():
    r = run(model="claude-sonnet-5-5", memory_retrievals_per_run=10)
    assert (r["shape"]["input_tokens_per_run"], r["shape"]["output_tokens_per_run"]) == (
        97_200,
        3_200,
    )  # estimate_agent_cost's default 8-step run
    memory = {x["platform"]: x["per_run_usd"]["memory"] for x in r["platforms"]}
    assert memory["agentcore"] == memory["vertex_agent_engine"] == 0.005  # 10 x 0.50 / 1,000
    assert memory["lambda"] is None  # no managed memory to price


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"runs_per_month": 0}, "runs_per_month"),
        ({"run_seconds": 0}, "run_seconds"),
        ({"active_cpu_share": 1.5}, "active_cpu_share"),
        ({"memory_gb": 0}, "memory_gb"),
        ({"tool_calls_per_run": -1}, "cannot be negative"),
    ],
)
def test_input_checks(kw, message):
    with pytest.raises(ValueError, match=message):
        run(**kw)


def test_compare_agent_platforms_is_a_public_tool():
    assert "compare_agent_platforms" in dispatch.tool_names()
    r = dispatch.call("compare_agent_platforms", {"runs_per_month": 1000, "model": "gpt-5"})
    assert r["kind"] == "agent_platform_comparison"
    assert "error" in dispatch.call("compare_agent_platforms", {"active_cpu_share": 0})
