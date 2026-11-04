"""analyze_ai_spend: AI spend by model and token type, against a fixed LLM catalogue.

Every figure is worked out by hand from MODELS and ARCHIVE below, so the weekly
LLM price refresh cannot move them.
"""

from __future__ import annotations

import pytest

from cloudprice_mcp import dispatch
from cloudprice_mcp.finops.bill_ai import analyze_ai_spend, model_key
from cloudprice_mcp.finops.focus_ingest import BillError
from tests.test_bill_insights import COLUMNS, row, write


def m(model_id, provider, inp, out, cache_read=None, cache_write=None):
    return {
        "litellm_id": model_id,
        "provider": provider,
        "mode": "chat",
        "input_per_1m_usd": inp,
        "output_per_1m_usd": out,
        "cache_read_per_1m_usd": cache_read,
        "cache_write_per_1m_usd": cache_write,
    }


MODELS = [
    m("anthropic.claude-sonnet-4-6", "bedrock", 3.0, 15.0, 0.3, 3.75),
    m("eu.anthropic.claude-sonnet-4-6", "bedrock", 3.3, 16.5, 0.33, 4.125),
    m("vertex_ai/claude-sonnet-4-6", "vertex", 3.0, 15.0, 0.3, 3.75),
    m("claude-sonnet-4-6", "anthropic", 3.0, 15.0, 0.3, 3.75),
    m("github_copilot/claude-sonnet-4.6", "github_copilot", 1.0, 1.0),  # not pay-per-token
    m("openai.gpt-oss-120b-1:0", "bedrock", 0.15, 0.6),
    m("baseten/openai/gpt-oss-120b", "baseten", 0.1, 0.5, 0.1),
    m("azure/gpt-4o-2024-05-13", "azure_openai", 5.0, 15.0),
    m("azure/gpt-4o", "azure_openai", 2.5, 10.0, 1.25),  # the undated alias is cheaper
    m("gpt-4o-2024-05-13", "openai", 5.0, 15.0),
]
ARCHIVE = [
    ("2026-10-01", {"openai.gpt-oss-120b-1:0": ["bedrock", 0.2, 0.8, None]}),
    ("2026-11-01", {"openai.gpt-oss-120b-1:0": ["bedrock", 0.15, 0.6, None]}),
]
COLS = [
    *COLUMNS, "x_UsageType", "SkuMeter", "ConsumedQuantity", "ConsumedUnit",
    "PricingQuantity", "PricingUnit",
]  # fmt: skip


def bedrock(usage_type, cost, tokens=None, **extra):
    return row(
        "2026-10-15",
        "Amazon Bedrock",
        cost,
        category="AI and Machine Learning",
        x_UsageType=usage_type,
        **({"ConsumedQuantity": tokens, "ConsumedUnit": "tokens"} if tokens else {}),
        **extra,
    )


def azure(meter, cost, thousands):
    return row(
        "2026-10-15",
        "Azure OpenAI",
        cost,
        category="AI and Machine Learning",
        ProviderName="Microsoft",
        SkuMeter=meter,
        ConsumedQuantity=thousands,
        ConsumedUnit="1K Tokens",
    )


@pytest.fixture
def ai_bill(tmp_path):
    rows = [
        # Claude Sonnet 4.6 on Bedrock, all four token types, at list price.
        bedrock("USE1-Claude4.6Sonnet-input-tokens", 30.0, 10e6),
        bedrock("USE1-Claude4.6Sonnet-output-tokens", 30.0, 2e6),
        bedrock("USE1-Claude4.6Sonnet-cache-read-input-token-count", 3.0, 10e6),
        bedrock("USE1-Claude4.6Sonnet-cache-write-input-token-count", 3.75, 1e6),
        # gpt-oss-120b: input counted in 1M blocks and paid 10% over list (priority).
        bedrock(
            "USE1-openai.gpt-oss-120b-mantle-input-tokens-standard",
            16.5,
            PricingQuantity=100,
            PricingUnit="1M tokens",
        ),
        bedrock("USE1-openai.gpt-oss-120b-mantle-output-tokens-standard", 12.0, 20e6),
        # Azure names the version date: the May 2024 model, not the cheaper alias.
        azure("gpt 4o 0513 Input global Tokens", 5.0, 1000),
        azure("gpt 4o 0513 Output global Tokens", 7.5, 500),
        # AI spend that is not priced per token.
        bedrock("USE1-Runtime-vCPU-Hours", 4.0, ServiceName="Amazon Bedrock AgentCore"),
        row("2026-10-15", "Amazon SageMaker", 6.0, category="AI and Machine Learning"),
        row("2026-10-15", "Amazon EC2", 50.0),  # not AI at all
    ]
    return str(write(tmp_path / "ai.csv", rows, columns=COLS))


def by_model(r):
    return {line["model"]: line for line in r["models"]}


def test_spend_by_model_and_token_type(ai_bill):
    r = analyze_ai_spend(ai_bill, models=MODELS, archive=ARCHIVE)
    assert r["totals"] == {
        "ai_cost": 117.75,
        "token_cost": 107.75,
        "other_ai_cost": 10.0,
        "tokens": {"input": 111e6, "output": 22.5e6, "cache_read": 10e6, "cache_write": 1e6},
        "cache_read_share_pct": 8.3,  # 10M of 121M input-side tokens
    }
    assert [line["model"] for line in r["models"]] == [
        "Claude4.6Sonnet",
        "openai.gpt-oss-120b",
        "gpt 4o 0513",
    ]
    claude = by_model(r)["Claude4.6Sonnet"]
    assert claude["cost_by_type"] == {
        "input": 30.0,
        "output": 30.0,
        "cache_read": 3.0,
        "cache_write": 3.75,
    }
    assert claude["catalogue_id"] == "anthropic.claude-sonnet-4-6"  # not the eu. variant
    assert (claude["list_cost_on_your_provider"], claude["paid_vs_list_pct"]) == (66.75, 0.0)
    assert claude["cache_read_share_pct"] == 50.0
    assert claude["cache_saving_per_10_points"] == 5.4  # 0.1 x 20M x (3.00 - 0.30) / 1M
    assert [(e["provider"], e["cost"]) for e in claude["same_model_elsewhere"]] == [
        ("anthropic", 66.75),
        ("vertex", 66.75),
    ]  # github_copilot is not pay-per-token
    assert r["other_ai_spend"] == [
        {"service": "Amazon SageMaker", "cost": 6.0},
        {"service": "Amazon Bedrock AgentCore", "cost": 4.0},
    ]


def test_list_price_premium_and_cheaper_provider(ai_bill):
    r = analyze_ai_spend(ai_bill, models=MODELS, archive=ARCHIVE)
    oss, gpt4o = by_model(r)["openai.gpt-oss-120b"], by_model(r)["gpt 4o 0513"]
    assert oss["tokens"]["input"] == 100e6  # 100 blocks of 1M
    assert (oss["list_cost_on_your_provider"], oss["paid_vs_list_pct"]) == (27.0, 5.6)
    assert oss["same_model_elsewhere"] == [
        {
            "provider": "baseten",
            "model_id": "baseten/openai/gpt-oss-120b",
            "cost": 20.0,
            "vs_your_provider_pct": -25.9,
        }
    ]
    assert gpt4o["catalogue_id"] == "azure/gpt-4o-2024-05-13"
    assert (gpt4o["list_cost_on_your_provider"], gpt4o["paid_vs_list_pct"]) == (12.5, 0.0)


def test_price_changes_and_headline(ai_bill):
    r = analyze_ai_spend(ai_bill, models=MODELS, archive=ARCHIVE)
    assert r["price_changes"] == [
        {
            "model_id": "openai.gpt-oss-120b-1:0",
            "from": "2026-10-01",
            "to": "2026-11-01",
            "input_per_1m": [0.2, 0.15],
            "output_per_1m": [0.8, 0.6],
            "your_token_mix_change_pct": -25.0,  # 36.00 -> 27.00 for 100M in, 20M out
        }
    ]
    assert r["headline"] == (
        "AI spend 117.75, of which 107.75 is tokens. Claude4.6Sonnet on AWS is 57% of it. "
        "8% of input tokens are cache reads; each 10 points more would save about 5.40. "
        "Same model, cheaper: baseten/openai/gpt-oss-120b on baseten, 26% less for the "
        "same tokens. 1 of your models changed list price since the bill began."
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Claude4.6Sonnet", "anthropic.claude-sonnet-4-6-v1:0"),
        ("Claude4.6Sonnet", "vertex_ai/claude-sonnet-4-6@default"),
        ("openai.gpt-oss-120b", "openai.gpt-oss-120b-1:0"),
        ("Nova2.0Lite", "amazon.nova-2-lite-v1:0"),
        ("Llama3.3-70B", "meta.llama3-3-70b-instruct-v1:0"),
        ("gpt 4o 0513", "azure/gpt-4o-2024-05-13"),
        ("Gemini 2.5 Pro", "gemini-2.5-pro"),
    ],
)
def test_bill_names_match_catalogue_ids(a, b):
    assert model_key(a) == model_key(b)


def test_unknown_units_and_models_are_reported(tmp_path):
    rows = [
        bedrock("USE1-MysteryModel9-input-tokens", 2.0, 1e6),
        azure("gpt 4o 0513 Input global Tokens", 5.0, 1000),
    ]
    rows[1]["ConsumedUnit"] = "Units"
    r = analyze_ai_spend(
        str(write(tmp_path / "u.csv", rows, columns=COLS)), models=MODELS, archive=ARCHIVE
    )
    assert any("MysteryModel9: not found" in w for w in r["warnings"])
    assert any("gpt 4o 0513: 5.00 of cost has no token count" in w for w in r["warnings"])
    assert by_model(r)["MysteryModel9"]["same_model_elsewhere"] == []


def test_input_checks(tmp_path):
    with pytest.raises(BillError, match="no AI rows"):
        analyze_ai_spend(
            str(write(tmp_path / "n.csv", [row("2026-10-01", "EC2", 5)])), models=MODELS
        )
    with pytest.raises(BillError, match="top_n"):
        analyze_ai_spend("x.csv", top_n=0, models=MODELS)


def test_analyze_ai_spend_is_local_only(ai_bill):
    args = {"file_path": ai_bill}
    assert "analyze_ai_spend" not in dispatch.tool_names()
    assert dispatch.call("analyze_ai_spend", args) == {"error": "Unknown tool: analyze_ai_spend"}
    assert dispatch.call("analyze_ai_spend", args, allow_local=True)["kind"] == "ai_spend"
    assert "error" in dispatch.call("analyze_ai_spend", {}, allow_local=True)
