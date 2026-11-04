"""compare_llm_vs_selfhost: server count, break-even and the never-breaks-even case."""

from __future__ import annotations

import math

import pytest

from cloudprice_mcp import dispatch
from cloudprice_mcp.finops import selfhost
from cloudprice_mcp.pricing import load_catalog

API = {
    "source": "litellm",
    "model_id": "m",
    "provider": "openai",
    "input_per_1m_usd": 1.0,
    "output_per_1m_usd": 4.0,
}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(selfhost, "_load_catalog", lambda: {"as_of": "x", "models": [API]})


def run(**kw):
    args = dict(
        model="m",
        monthly_input_tokens=1_000_000_000,
        monthly_output_tokens=250_000_000,
        gpu_type="H100",
        gpu_count=8,
        output_tokens_per_second=10_000,
        utilization=0.5,
    )
    args.update(kw)
    return selfhost.compare_llm_vs_selfhost(load_catalog(), **args)


def test_api_cost_and_server_count(api):
    r = run()
    assert r["api"]["monthly_usd"] == pytest.approx(2_000.0)  # 1000*1 + 250*4
    # load = 250M + 1000M/10 = 350M; capacity = 10k*3600*730*0.5 = 13.14B -> 1 server
    assert all(o["servers"] == 1 for o in r["self_host"])
    prices = [o["monthly_usd"] for o in r["self_host"]]
    assert prices == sorted(prices)


def test_break_even_when_a_full_server_beats_the_api(api):
    r = run()
    be = r["break_even"]
    # API $2000 / 350M units = $5.71/1M; a full server is far cheaper per unit.
    assert be["reachable"] is True
    one_server = r["self_host"][0]["server_hourly_usd"] * 730
    assert be["volume_multiple_of_today"] == pytest.approx(one_server / 2_000, rel=1e-3)


def test_never_breaks_even_when_throughput_is_too_low(api):
    r = run(output_tokens_per_second=50)
    assert r["break_even"]["reachable"] is False
    assert r["cheaper"] == "api"
    assert "never breaks even" in r["headline"]


def test_volume_needing_several_servers(api):
    r = run(monthly_output_tokens=40_000_000_000, monthly_input_tokens=0)
    assert r["self_host"][0]["servers"] == math.ceil(40e9 / 13.14e9)


def test_validation_and_missing_args():
    assert "required" in dispatch.call("compare_llm_vs_selfhost", {"model": "gpt-5"})["error"]
    r = dispatch.call(
        "compare_llm_vs_selfhost",
        {"model": "gpt-5", "gpu_type": "H100", "output_tokens_per_second": 100},
    )
    assert "volume" in r["error"]
    r = dispatch.call(
        "compare_llm_vs_selfhost",
        {
            "model": "gpt-5",
            "gpu_type": "B999",
            "monthly_output_tokens": 1,
            "output_tokens_per_second": 100,
        },
    )
    assert "no cloud" in r["error"]
