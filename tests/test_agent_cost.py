"""estimate_agent_cost: context growth, caching and alternatives, checked by hand."""

from __future__ import annotations

import pytest

from cloudprice_mcp import dispatch
from cloudprice_mcp.finops import agent_cost

ROW = {
    "source": "litellm",
    "model_id": "m",
    "provider": "anthropic",
    "input_per_1m_usd": 2.0,
    "output_per_1m_usd": 10.0,
    "cache_read_per_1m_usd": 0.1,
    "cache_write_per_1m_usd": 2.5,
    "context_window_tokens": 20_000,
}
CHEAP = {**ROW, "model_id": "cheap", "input_per_1m_usd": 0.2, "output_per_1m_usd": 1.0}
NOCACHE = {**ROW, "model_id": "nocache", "cache_read_per_1m_usd": None}


@pytest.fixture
def catalog(monkeypatch):
    fake = {"as_of": "2026-11-04", "models": [ROW, CHEAP, NOCACHE]}
    monkeypatch.setattr(agent_cost, "_load_catalog", lambda: fake)


def test_hand_checked_run(catalog):
    # 8 steps, prefix 5,500, growth 1,900/step: inputs 5,500 .. 18,800, sum 97,200.
    r = agent_cost.estimate_agent_cost(model="m", compare_models=[])
    assert r["per_run"]["input_tokens"] == 97_200
    assert r["per_run"]["uncached_usd"] == pytest.approx(0.2264)
    # write 5,500 @2.5 + read 78,400 @0.1 + write 13,300 @2.5 + output 3,200 @10
    assert r["per_run"]["cached_usd"] == pytest.approx(0.08684)
    assert r["monthly_usd"] == pytest.approx(868.4)
    assert r["context_growth"]["share_of_input_from_growth"] == pytest.approx(0.547, abs=1e-3)


def test_one_step_has_no_growth(catalog):
    r = agent_cost.estimate_agent_cost(model="m", steps=1, compare_models=[])
    assert r["context_growth"]["share_of_input_from_growth"] == 0


def test_no_cache_price_means_no_cached_figure(catalog):
    r = agent_cost.estimate_agent_cost(model="nocache", compare_models=[])
    assert r["per_run"]["cached_usd"] is None
    assert r["caching_saves_pct"] is None
    assert r["per_run"]["best_usd"] == r["per_run"]["uncached_usd"]


def test_alternatives_sorted_cheapest_first_and_skip_self_and_unknown(catalog):
    r = agent_cost.estimate_agent_cost(model="m", compare_models=["nocache", "cheap", "m", "x"])
    assert [a["model_id"] for a in r["alternatives"]] == ["cheap", "nocache"]


def test_context_window_overflow_warns(catalog):
    r = agent_cost.estimate_agent_cost(model="m", steps=12, compare_models=[])
    assert "context window" in r["warnings"][0]


def test_bad_input_is_an_error_payload():
    assert "required" in dispatch.call("estimate_agent_cost", {})["error"]
    assert "not found" in dispatch.call("estimate_agent_cost", {"model": "no-such"})["error"]
    r = dispatch.call("estimate_agent_cost", {"model": "gpt-5", "steps": 0})
    assert "at least 1" in r["error"]


def test_real_catalogue_default_comparison():
    r = dispatch.call("estimate_agent_cost", {"model": "claude-sonnet-5-5"})
    assert r["model"]["provider"] == "anthropic"
    assert len(r["alternatives"]) >= 4


def test_hosting_hand_checked(catalog):
    # 36 s run, 1 vCPU active 25%, 2 GB, 7 tool calls, 4 retrievals, 10 events.
    r = agent_cost.estimate_agent_cost(
        model="m",
        compare_models=[],
        hosting={
            "run_seconds": 36,
            "active_cpu_share": 0.25,
            "memory_retrievals_per_run": 4,
            "memory_events_per_run": 10,
        },
    )
    h = r["hosting"]
    # runtime: 1*0.01h*0.25*0.1276 + 2*0.01h*0.0169 = 0.000319 + 0.000338
    assert h["agentcore"]["runtime_usd"] == pytest.approx(0.000657, abs=1e-6)
    assert h["agentcore"]["gateway_usd"] == pytest.approx(7 * 0.005 / 1000)
    # memory: 4 * 0.5/1000 + 10 * 12 KB at $1/GB
    assert h["agentcore"]["memory_usd"] == pytest.approx(0.002 + 0.00012)
    # lambda: 2 GB * 36 s * 0.0000166667 + 0.2/1M
    assert h["lambda"]["hosting_per_run_usd"] == pytest.approx(0.0012002, abs=1e-6)
    assert h["cheaper_compute"] == "agentcore_runtime"
    total = h["agentcore"]["total_per_run_usd"]
    assert total == pytest.approx(r["per_run"]["best_usd"] + h["agentcore"]["hosting_per_run_usd"])


def test_hosting_long_run_warns_about_lambda_limit(catalog):
    r = agent_cost.estimate_agent_cost(model="m", compare_models=[], hosting={"run_seconds": 1200})
    assert any("15-minute" in w for w in r["warnings"])


def test_hosting_requires_run_seconds(catalog):
    with pytest.raises(ValueError, match="run_seconds"):
        agent_cost.estimate_agent_cost(model="m", compare_models=[], hosting={"vcpu": 2})


def test_no_hosting_by_default(catalog):
    assert agent_cost.estimate_agent_cost(model="m", compare_models=[])["hosting"] is None
