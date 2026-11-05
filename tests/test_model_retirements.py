"""model_retirements: retirement dates and same-family replacements.

A fixed catalogue and a fixed today (2026-11-04), so the weekly refresh cannot
move any figure. Price changes are list prices at 3 input to 1 output token.
"""

from __future__ import annotations

from datetime import date

import pytest

from cloudprice_mcp import dispatch
from cloudprice_mcp.finops.bill_ai import analyze_ai_spend
from cloudprice_mcp.finops.model_retirements import Radar, model_retirements
from tests.test_bill_ai import COLS, bedrock
from tests.test_bill_insights import write

TODAY = date(2026, 11, 4)


def m(model_id, provider, inp, out, retires_on=None, mode="chat"):
    row = {
        "litellm_id": model_id,
        "provider": provider,
        "mode": mode,
        "input_per_1m_usd": inp,
        "output_per_1m_usd": out,
    }
    if retires_on:
        row["retires_on"] = retires_on
    return row


CATALOGUE = [
    m("anthropic.claude-3-5-sonnet-20240620-v1:0", "bedrock", 3.0, 15.0, "2026-12-01"),
    m("us.anthropic.claude-3-5-sonnet-20240620-v1:0", "bedrock", 3.3, 16.5, "2026-12-01"),
    m("anthropic.claude-3-7-sonnet-20250219-v1:0", "bedrock", 3.0, 15.0, "2027-01-15"),
    m("anthropic.claude-sonnet-4-6", "bedrock", 3.0, 15.0),
    m("anthropic.claude-sonnet-5-5", "bedrock", 2.0, 10.0),  # newest: the replacement
    m("azure/gpt-4o-2024-05-13", "azure_openai", 5.0, 15.0, "2026-10-20"),  # retired 15 days ago
    m("azure/gpt-4o-2024-11-20", "azure_openai", 2.5, 10.0),
    m("gpt-4o-2024-05-13", "openai", 5.0, 15.0, "2027-06-01"),  # outside 180 days
    m("vertex_ai/claude-3-haiku", "vertex", 0.25, 1.25, "2026-06-01"),  # retired too long ago
    m("github_copilot/gpt-4o", "github_copilot", 1.0, 1.0, "2026-11-10"),  # not pay-per-token
    m("amazon.titan-embed-text-v1", "bedrock", 0.1, 0.0, "2026-11-20", mode="embedding"),
]


def run(**kw):
    return model_retirements(models=CATALOGUE, today=TODAY, **kw)


def test_retiring_models_soonest_first_with_replacements():
    r = run()
    assert [(e["model_id"], e["days_left"], e["status"]) for e in r["models"]] == [
        ("azure/gpt-4o-2024-05-13", -15, "retired"),
        ("anthropic.claude-3-5-sonnet-20240620-v1:0", 27, "retiring"),
        ("anthropic.claude-3-7-sonnet-20250219-v1:0", 72, "retiring"),
    ]
    gpt, sonnet35, sonnet37 = r["models"]
    assert sonnet35["regional_variants"] == 2  # the us. profile is the same model
    # 3.7 retires inside the window too, so it is never offered as the replacement.
    assert sonnet35["replacement"] == {
        "model_id": "anthropic.claude-sonnet-5-5",
        "retires_on": None,
        "price_change_pct": -33.3,  # (3x3 + 15)/4 = 6.00 -> (3x2 + 10)/4 = 4.00
    }
    assert sonnet37["replacement"]["model_id"] == "anthropic.claude-sonnet-5-5"
    assert gpt["replacement"] == {
        "model_id": "azure/gpt-4o-2024-11-20",  # newer version date, same family
        "retires_on": None,
        "price_change_pct": -41.7,  # 7.50 -> 4.375
    }
    assert r["by_provider"] == {"bedrock": 2, "azure_openai": 1}
    assert r["headline"] == (
        "3 models retire within 180 days or retired recently (bedrock 2, azure_openai 1). "
        "Next: anthropic.claude-3-5-sonnet-20240620-v1:0 on bedrock, 2026-12-01; "
        "newest same-family model: anthropic.claude-sonnet-5-5, -33% list price."
    )


def test_filters_and_window():
    assert [e["provider"] for e in run(provider="Azure_OpenAI")["models"]] == ["azure_openai"]
    assert [e["model_id"] for e in run(model="claude 3.5 sonnet")["models"]] == [
        "anthropic.claude-3-5-sonnet-20240620-v1:0"
    ]
    assert run(within_days=1095)["count"] == 4  # adds OpenAI's gpt-4o in June 2027
    assert run(provider="nobody")["headline"] == (
        "No model in the catalogue retires in the next 180 days."
    )
    with pytest.raises(ValueError, match="within_days"):
        run(within_days=0)
    with pytest.raises(ValueError, match="top_n"):
        run(top_n=500)


def test_lookup_matches_bill_and_config_names():
    radar = Radar(CATALOGUE, TODAY)
    hit = radar.lookup("Claude3.5Sonnet", ("bedrock",))
    assert (hit["model_id"], hit["days_left"]) == ("anthropic.claude-3-5-sonnet-20240620-v1:0", 27)
    assert radar.lookup("us.anthropic.claude-3-5-sonnet-20240620-v1:0")["provider"] == "bedrock"
    assert radar.lookup("anthropic.claude-sonnet-5-5") is None  # no date known
    assert radar.lookup("Claude3.5Sonnet", ("azure_openai",)) is None


def test_analyze_ai_spend_flags_a_retiring_model(tmp_path):
    rows = [bedrock("USE1-Claude3.5Sonnet-input-tokens", 30.0, 10e6)]
    bill = str(write(tmp_path / "ai.csv", rows, columns=COLS))
    r = analyze_ai_spend(bill, models=CATALOGUE, archive=[], today=TODAY)
    assert [(e["model"], e["retires_on"], e["days_left"]) for e in r["retiring_models"]] == [
        ("Claude3.5Sonnet", "2026-12-01", 27)
    ]
    assert r["models"][0]["retirement"]["replacement"]["model_id"] == "anthropic.claude-sonnet-5-5"
    assert r["headline"].endswith(" Claude3.5Sonnet retires on 2026-12-01.")


def test_model_retirements_is_a_public_tool():
    assert "model_retirements" in dispatch.tool_names()
    r = dispatch.call("model_retirements", {"within_days": 30, "top_n": 5})
    assert r["kind"] == "model_retirements"
    assert "error" in dispatch.call("model_retirements", {"within_days": 0})
