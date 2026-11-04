"""analyze_ai_spend — FinOps for AI on the user's actual bill.

Reads the generative-AI lines of a FOCUS export (Amazon Bedrock, Azure OpenAI
and AI Foundry, Vertex AI, OCI Generative AI) and answers:

  - what each model costs, split into input, output, cache-read and cache-write
    tokens, and what you paid against its list price on your provider;
  - what the same token mix costs for the same model on other providers, from
    the LLM catalogue (Bedrock vs Azure vs Vertex vs the model maker's API);
  - how much input is served from prompt cache, and what more would save;
  - which of your models changed price in the weekly LLM price archive.

Same privacy rules as analyze_bill: local file, no network, local-only tool.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .. import llm_history
from .bill_insights import _CLOUD_OF, _load
from .extended_tokens import _load_catalog
from .focus_ingest import BillError, _num, row_cost

TOKEN_TYPES = ("input", "output", "cache_read", "cache_write")
_PRICE_KEYS = {
    "input": "input_per_1m_usd",
    "output": "output_per_1m_usd",
    "cache_read": "cache_read_per_1m_usd",
    "cache_write": "cache_write_per_1m_usd",
}
_AI_SERVICE = re.compile(
    r"bedrock|openai|ai foundry|foundry models|cognitive services|vertex|gemini|generative ai",
    re.I,
)
# Columns that name the model, in the order they are trusted.
_TEXT_COLUMNS = (
    "x_UsageType",  # AWS: USE1-Claude4.6Sonnet-input-tokens
    "SkuMeter",  # Azure: gpt 4o 0513 Input global Tokens
    "x_SkuMeterName",
    "x_SkuMeterSubcategory",
    "x_SkuDescription",
    "ChargeDescription",
)
# Checked in order: cache reads are input tokens too, so they come first.
_TOKEN_TYPE = (
    (
        "cache_read",
        re.compile(r"cache[-_ ]?read|cached[-_ ]?input|cache[-_ ]?hit|\bcached\b", re.I),
    ),
    ("cache_write", re.compile(r"cache[-_ ]?write|cache[-_ ]?creation", re.I)),
    ("output", re.compile(r"output|completion|generated", re.I)),
    ("input", re.compile(r"input|prompt", re.I)),
)
_MODEL_END = re.compile(
    r"[-_ ](?:mantle[-_ ])?(?:cache|cached|input|output|prompt|completion)\b", re.I
)
_PROVIDERS_OF = {
    "aws": ("bedrock", "bedrock_converse"),
    "azure": ("azure_openai", "azure_ai", "azure"),
    "gcp": ("vertex", "vertex_ai", "google"),
    "oci": ("oci",),
}
_NOT_PAY_PER_TOKEN = frozenset({"github_copilot"})
_GEO_ID = re.compile(r"(^|[/.])(us|eu|apac|au|jp|ca|us-gov|global)[./]|gov-|/us-|/eu-", re.I)
_MAKERS = (
    "anthropic|amazon|openai|meta|mistral|cohere|ai21|deepseek|qwen|google|writer|"
    "twelvelabs|stability|minimax|moonshot|nvidia|xai"
)
_NOISE = frozenset(
    [
        "tokens",
        "token",
        "global",
        "regional",
        "datazone",
        "data",
        "zone",
        "standard",
        "batch",
        "priority",
        "flex",
        "cross",
        "region",
        "provisioned",
        "text",
        "predictions",
        "count",
        "per",
        "mantle",
        "models",
        "model",
        "instruct",
        "chat",
        "latest",
        "default",
    ]
)


# --- model names -------------------------------------------------------------


def model_key(name: str) -> frozenset[str]:
    """Order-free key that bill names and catalogue IDs share.

    'Claude4.6Sonnet', 'anthropic.claude-sonnet-4-6-v1:0' and
    'vertex_ai/claude-sonnet-4-6@default' all give {'claude', 'sonnet', '4.6'}.
    A version date becomes a '@MMDD' token, so Azure's 'gpt 4o 0513' and
    'gpt-4o-2024-05-13' agree; _plain() drops it to match undated names.
    """
    s = name.lower().rsplit("/", 1)[-1]
    s = re.sub(r"@.*$", "", s)
    s = re.sub(r"^(us|eu|apac|au|jp|ca|global|us-gov)\.", "", s)
    s = re.sub(rf"^({_MAKERS})\.", "", s)
    s = re.sub(r"-\d+:\d+$", "", s)  # openai.gpt-oss-120b-1:0
    s = re.sub(r"(-v\d+)?(:\d+)?$", "", s)  # amazon.nova-2-lite-v1:0
    dates = [m[-4:] for m in re.findall(r"\b\d{8}\b", s)]
    dates += [y + d for _, y, d in re.findall(r"(\d{4})-(\d{2})-(\d{2})", s)]
    s = re.sub(r"\d{4}-\d{2}-\d{2}|\b\d{8}\b", " ", s)
    s = re.sub(r"(?<=[a-z])(?=\d)", " ", s)  # claude4.6 -> claude 4.6
    s = re.sub(r"(?<=\d)(?=[a-z]{2,})", " ", s)  # 4.6sonnet -> 4.6 sonnet; 4o stays
    parts = [p for p in re.split(r"[^a-z0-9.]+|(?<!\d)\.|\.(?!\d)", s) if p]
    tokens: list[str] = []
    for p in parts:
        if p.isdigit() and len(p) == 4:
            dates.append(p)  # Azure's 0513-style version dates
        elif p.isdigit() and tokens and re.fullmatch(r"\d+(\.\d+)*", tokens[-1]):
            tokens[-1] += "." + p  # claude-sonnet-4-6 -> 4.6
        else:
            tokens.append(p)
    tokens = [t[:-2] if re.fullmatch(r"\d+\.0", t) else t for t in tokens]
    return frozenset([t for t in tokens if t not in _NOISE] + ["@" + d for d in dates])


def _plain(key: frozenset[str]) -> frozenset[str]:
    return frozenset(t for t in key if not t.startswith("@"))


def _model_text(r: dict[str, str]) -> str | None:
    for col in _TEXT_COLUMNS:
        text = (r.get(col) or "").strip()
        if not text:
            continue
        if col == "x_UsageType":
            text = re.sub(r"^[A-Z]{2,4}\d-", "", text)  # USE1-
        name = _MODEL_END.split(text, maxsplit=1)[0].strip(" -_")
        if col == "x_UsageType" and name == text:
            continue  # not a token usage type
        return name or None
    return None


def _token_type(text: str) -> str | None:
    if not re.search(r"token", text, re.I):
        return None
    return next((kind for kind, rx in _TOKEN_TYPE if rx.search(text)), None)


def _token_count(r: dict[str, str]) -> float | None:
    for qty_col, unit_col in (
        ("ConsumedQuantity", "ConsumedUnit"),
        ("PricingQuantity", "PricingUnit"),
    ):
        qty = _num(r.get(qty_col))
        unit = (r.get(unit_col) or "").lower()
        if qty is None or not unit:
            continue
        if re.search(r"\b1m\b|million|1,000,000", unit):
            return qty * 1e6
        if re.search(r"\b1k\b|thousand|\b1,?000\b", unit):
            return qty * 1e3
        if "token" in unit:
            return qty
    return None


# --- catalogue ---------------------------------------------------------------


def _index(models: list[dict[str, Any]]) -> dict[frozenset[str], list[dict[str, Any]]]:
    index: dict[frozenset[str], list[dict[str, Any]]] = defaultdict(list)
    for m in models:
        if m.get("mode") not in (None, "chat", "completion", "responses"):
            continue
        if m.get("provider") in _NOT_PAY_PER_TOKEN or m.get("input_per_1m_usd") is None:
            continue
        model_id = m.get("model_id") or m.get("litellm_id") or ""
        key = model_key(model_id)
        row = {**m, "model_id": model_id}
        index[key].append(row)
        if _plain(key) != key:
            index[_plain(key)].append(row)
    return index


def _base_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The plain list price: no geography prefix or gov region, shortest ID."""
    plain = [m for m in rows if not _GEO_ID.search(m["model_id"])] or rows
    return min(plain, key=lambda m: (len(m["model_id"]), m["model_id"]))


def _cost(m: dict[str, Any], tokens: dict[str, float]) -> float:
    total = 0.0
    for kind, count in tokens.items():
        price = m.get(_PRICE_KEYS[kind])
        if price is None:  # no cache price listed: charge it as input
            price = m["input_per_1m_usd"]
        total += count * price / 1e6
    return total


# --- the tool ----------------------------------------------------------------


def analyze_ai_spend(
    file_path: str,
    top_n: int = 10,
    *,
    models: list[dict[str, Any]] | None = None,
    archive: list[tuple[str, dict[str, list[Any]]]] | None = None,
) -> dict[str, Any]:
    """`models` and `archive` default to the bundled LLM catalogue and price archive."""
    if not 1 <= top_n <= 50:
        raise BillError("top_n must be between 1 and 50")
    rows, warnings = _load(file_path)
    return ai_spend_from_rows(
        rows, warnings, Path(file_path).name, top_n, models=models, archive=archive
    )


def ai_spend_from_rows(
    rows: list[dict[str, str]],
    warnings: list[str],
    file_name: str,
    top_n: int = 10,
    *,
    models: list[dict[str, Any]] | None = None,
    archive: list[tuple[str, dict[str, list[Any]]]] | None = None,
) -> dict[str, Any]:
    """analyze_ai_spend on rows already read, e.g. narrowed to one agent's tag."""
    if models is None:
        catalogue = _load_catalog()
        models = [m for m in catalogue["models"] if m.get("source") == "litellm"]
    index = _index(models)

    lines: dict[tuple[str, str], dict[str, Any]] = {}
    other: dict[str, float] = defaultdict(float)
    days: set[str] = set()
    ai_cost = 0.0
    for r in rows:
        cost = row_cost(r)
        if cost is None or (r.get("ChargeCategory") or "Usage") != "Usage":
            continue
        service = r.get("ServiceName") or ""
        if r.get("ServiceCategory") != "AI and Machine Learning" and not _AI_SERVICE.search(
            service
        ):
            continue
        ai_cost += cost
        days.add((r.get("ChargePeriodStart") or "")[:10])
        text = " ".join(r.get(c) or "" for c in _TEXT_COLUMNS)
        kind, name = _token_type(text), _model_text(r)
        if not kind or not name:
            other[service or "(blank)"] += cost
            continue
        cloud = _CLOUD_OF.get((r.get("ProviderName") or "").strip().lower(), "other")
        line = lines.setdefault(
            (cloud, " ".join(sorted(model_key(name))) or name.lower()),
            {
                "model": name,
                "cloud": cloud,
                "cost": 0.0,
                "tokens": dict.fromkeys(TOKEN_TYPES, 0.0),
                "cost_by_type": dict.fromkeys(TOKEN_TYPES, 0.0),
                "uncounted_cost": 0.0,
            },
        )
        line["cost"] += cost
        line["cost_by_type"][kind] += cost
        count = _token_count(r)
        if count is None:
            line["uncounted_cost"] += cost
        else:
            line["tokens"][kind] += count

    if not ai_cost:
        raise BillError("no AI rows (Bedrock, Azure OpenAI, Vertex AI, OCI Generative AI) found")

    result_lines = [_price_line(line, index, warnings) for line in lines.values()]
    result_lines.sort(key=lambda line: line["cost"], reverse=True)
    token_cost = sum(line["cost"] for line in result_lines)
    tokens = {k: sum(line["tokens"][k] for line in result_lines) for k in TOKEN_TYPES}
    period = {"from": min(days - {""}, default=None), "to": max(days - {""}, default=None)}
    changes = _price_changes(result_lines, archive, period["from"], warnings)

    result = {
        "kind": "ai_spend",
        "file": file_name,
        "period": period,
        "totals": {
            "ai_cost": round(ai_cost, 2),
            "token_cost": round(token_cost, 2),
            "other_ai_cost": round(ai_cost - token_cost, 2),
            "tokens": {k: round(v) for k, v in tokens.items()},
            "cache_read_share_pct": _share(tokens["cache_read"], tokens["input"]),
        },
        "models": result_lines[:top_n],
        "price_changes": changes,
        "other_ai_spend": [
            {"service": s, "cost": round(c, 2)}
            for s, c in sorted(other.items(), key=lambda kv: -kv[1])
        ],
        "warnings": warnings,
        "honest_gaps": [
            "Token-priced generative AI only. Provisioned throughput, model units, "
            "fine-tuning, embeddings priced per character and agent hosting are "
            "listed under other_ai_spend with their cost, not repriced.",
            "Alternatives are the same model on other providers, at list price from "
            "the LiteLLM catalogue. A cheaper but different model is a quality "
            "decision this tool does not make.",
            "List prices are the base (no geography prefix) rate. Regional and "
            "cross-region routing, batch, flex and priority tiers change the price; "
            "paid_vs_list_pct shows how far your bill is from list.",
            "Prompt caching savings assume the cached part of the prompt really "
            "repeats. Cache writes cost more than plain input on some models, so "
            "short-lived caches can cost more than they save.",
            "Models are matched by name. An unmatched model is still reported with "
            "its spend and tokens, but without list prices.",
        ],
    }
    result["headline"] = _headline(result)
    return result


def _share(part: float, other: float) -> float | None:
    return round(part / (part + other) * 100, 1) if part + other else None


def _price_line(
    line: dict[str, Any], index: dict[frozenset[str], list[dict[str, Any]]], warnings: list[str]
) -> dict[str, Any]:
    tokens = {k: v for k, v in line["tokens"].items() if v}
    total_tokens = sum(tokens.values())
    out: dict[str, Any] = {
        "model": line["model"],
        "cloud": line["cloud"],
        "cost": round(line["cost"], 2),
        "tokens": {k: round(v) for k, v in line["tokens"].items()},
        "cost_by_type": {k: round(v, 2) for k, v in line["cost_by_type"].items()},
        "paid_per_1m_tokens": (
            round(line["cost"] / total_tokens * 1e6, 4) if total_tokens else None
        ),
        "cache_read_share_pct": _share(line["tokens"]["cache_read"], line["tokens"]["input"]),
        "catalogue_id": None,
        "list_cost_on_your_provider": None,
        "paid_vs_list_pct": None,
        "same_model_elsewhere": [],
        "cache_saving_per_10_points": None,
    }
    if line["uncounted_cost"]:
        warnings.append(
            f"{line['model']}: {line['uncounted_cost']:,.2f} of cost has no token count "
            "(unit not tokens, 1K or 1M tokens)"
        )
    key = model_key(line["model"])
    matches = index.get(key) or index.get(_plain(key), [])
    if not matches:
        warnings.append(f"{line['model']}: not found in the LLM catalogue; no list prices")
        return out

    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in matches:
        by_provider[m["provider"]].append(m)
    priced = {p: _base_row(rows) for p, rows in by_provider.items()}
    own = next((priced[p] for p in _PROVIDERS_OF.get(line["cloud"], ()) if p in priced), None)
    if own and total_tokens:
        list_cost = _cost(own, tokens)
        counted = line["cost"] - line["uncounted_cost"]
        out["catalogue_id"] = own["model_id"]
        out["list_cost_on_your_provider"] = round(list_cost, 2)
        if list_cost:
            out["paid_vs_list_pct"] = round((counted / list_cost - 1) * 100, 1)
        cache_price = own.get("cache_read_per_1m_usd")
        pool = line["tokens"]["input"] + line["tokens"]["cache_read"]
        if cache_price is not None and pool:
            out["cache_saving_per_10_points"] = round(
                0.1 * pool * (own["input_per_1m_usd"] - cache_price) / 1e6, 2
            )
    if total_tokens:
        base = out["list_cost_on_your_provider"]
        elsewhere = sorted(
            (
                {
                    "provider": p,
                    "model_id": m["model_id"],
                    "cost": round(_cost(m, tokens), 2),
                }
                for p, m in priced.items()
                if not own or p != own["provider"]
            ),
            key=lambda e: (e["cost"], e["provider"]),
        )[:5]
        for e in elsewhere:
            e["vs_your_provider_pct"] = round((e["cost"] / base - 1) * 100, 1) if base else None
        out["same_model_elsewhere"] = elsewhere
    return out


def _price_changes(
    lines: list[dict[str, Any]],
    archive: list[tuple[str, dict[str, list[Any]]]] | None,
    since: str | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """List price moves, between the bill's first week and the latest archived week."""
    ids = {line["catalogue_id"]: line for line in lines if line["catalogue_id"]}
    if not ids:
        return []
    if archive is None:
        dates = llm_history.list_dates()
        if len(dates) < 2:
            return []
        earlier = [d for d in dates if since and d <= since] or dates[:1]
        archive = [
            (d, llm_history.load(d)["sources"].get("litellm", {})) for d in (earlier[-1], dates[-1])
        ]
    (then_date, then), (now_date, now) = archive[0], archive[-1]
    if then_date == now_date:
        return []
    changes = []
    for model_id, line in ids.items():
        old, new = then.get(model_id), now.get(model_id)
        if not old or not new or old[1:3] == new[1:3]:
            continue
        mix = {k: v for k, v in line["tokens"].items() if v}
        old_row = {
            "input_per_1m_usd": old[1],
            "output_per_1m_usd": old[2],
            "cache_read_per_1m_usd": old[3],
        }
        new_row = {
            "input_per_1m_usd": new[1],
            "output_per_1m_usd": new[2],
            "cache_read_per_1m_usd": new[3],
        }
        mix.pop("cache_write", None)  # the archive does not keep cache-write prices
        before, after = _cost(old_row, mix), _cost(new_row, mix)
        changes.append(
            {
                "model_id": model_id,
                "from": then_date,
                "to": now_date,
                "input_per_1m": [old[1], new[1]],
                "output_per_1m": [old[2], new[2]],
                "your_token_mix_change_pct": round((after / before - 1) * 100, 1)
                if before
                else None,
            }
        )
    if since and since < archive[0][0]:
        warnings.append(f"the price archive starts {archive[0][0]}, after this bill begins")
    return changes


_NAMES = {"aws": "AWS", "azure": "Azure", "gcp": "GCP", "oci": "OCI", "other": "another provider"}


def _headline(r: dict[str, Any]) -> str:
    t = r["totals"]
    line = f"AI spend {t['ai_cost']:,.2f}, of which {t['token_cost']:,.2f} is tokens."
    if not r["models"]:
        return line + " No token-priced model lines were found; see other_ai_spend."
    top = r["models"][0]
    share = top["cost"] / t["ai_cost"] * 100 if t["ai_cost"] else 0
    line += f" {top['model']} on {_NAMES[top['cloud']]} is {share:.0f}% of it."
    if t["cache_read_share_pct"] is not None:
        line += f" {t['cache_read_share_pct']:.0f}% of input tokens are cache reads"
        saving = sum(m["cache_saving_per_10_points"] or 0 for m in r["models"])
        line += f"; each 10 points more would save about {saving:,.2f}." if saving else "."
    best = min(
        (
            e
            for m in r["models"]
            for e in m["same_model_elsewhere"]
            if e["vs_your_provider_pct"] is not None
        ),
        key=lambda e: e["vs_your_provider_pct"],
        default=None,
    )
    if best and best["vs_your_provider_pct"] <= -5:
        line += (
            f" Same model, cheaper: {best['model_id']} on {best['provider']}, "
            f"{-best['vs_your_provider_pct']:.0f}% less for the same tokens."
        )
    if r["price_changes"]:
        line += (
            f" {len(r['price_changes'])} of your models changed list price since the bill began."
        )
    return line
