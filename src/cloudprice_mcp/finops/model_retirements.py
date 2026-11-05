"""model_retirements — which LLMs providers are retiring, and what replaces them.

LiteLLM's public price feed records the day each provider retires a model
(Bedrock, Azure OpenAI and AI Foundry, Vertex AI, OpenAI, Anthropic and more);
the weekly catalogue refresh keeps it as `retires_on`. This tool lists the models
retiring within a window and, for each, the newest model of the same family on
the same provider that is not retiring, with the price difference.

analyze_ai_spend and review_agentcore call Radar.lookup() to flag retiring models
on a bill and in a deployment.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from .bill_ai import _GEO_ID, _NOT_PAY_PER_TOKEN, _plain, model_key
from .extended_tokens import _load_catalog

WINDOW_DAYS = 180
RECENTLY_RETIRED_DAYS = 90
_NUMBER = re.compile(r"\d+(\.\d+)*")
_FULL_DATE = re.compile(r"(20\d{2})-?(\d{2})-?(\d{2})")
_GEO_PREFIX = re.compile(r"^(?:[a-z_]+/)?(us|eu|apac|au|jp|ca|global|us-gov)\.", re.I)


def _family(key: frozenset[str]) -> frozenset[str]:
    """The name without versions or dates: {'claude', 'sonnet'}, {'gpt', '4o', 'mini'}."""
    return frozenset(t for t in key if not t.startswith("@") and not _NUMBER.fullmatch(t))


def _version(model_id: str, key: frozenset[str]) -> tuple[list[tuple[int, ...]], str]:
    numbers = sorted(
        (tuple(int(x) for x in t.split(".")) for t in key if _NUMBER.fullmatch(t)), reverse=True
    )
    return numbers, max(("".join(d) for d in _FULL_DATE.findall(model_id)), default="")


def _blend(m: dict[str, Any]) -> float:
    """Price per 1M tokens at 3 input to 1 output, a typical chat mix."""
    return (3 * float(m["input_per_1m_usd"]) + float(m.get("output_per_1m_usd") or 0)) / 4


def _geo(model_id: str) -> str:
    match = _GEO_PREFIX.match(model_id)
    return match.group(1).lower() if match else ""


class Radar:
    """Retirement dates and same-family replacements over the LLM catalogue."""

    def __init__(
        self,
        models: list[dict[str, Any]] | None = None,
        today: date | None = None,
        window_days: int = WINDOW_DAYS,
    ) -> None:
        if models is None:
            models = [m for m in _load_catalog()["models"] if m.get("source") == "litellm"]
        self.today = today or date.today()
        self.window_end = (self.today + timedelta(days=window_days)).isoformat()
        self.rows: list[dict[str, Any]] = []
        self.by_key: dict[frozenset[str], list[dict[str, Any]]] = defaultdict(list)
        self.by_family: dict[tuple[str, frozenset[str]], list[dict[str, Any]]] = defaultdict(list)
        for m in models:
            if m.get("mode") not in (None, "chat", "completion", "responses"):
                continue
            if m.get("provider") in _NOT_PAY_PER_TOKEN or m.get("input_per_1m_usd") is None:
                continue
            model_id = m.get("model_id") or m.get("litellm_id") or ""
            key = model_key(model_id)
            row = {**m, "model_id": model_id, "_key": key}
            self.rows.append(row)
            self.by_key[key].append(row)
            if _plain(key) != key:
                self.by_key[_plain(key)].append(row)
            self.by_family[(m["provider"], _family(key))].append(row)

    def days_left(self, retires_on: str) -> int | None:
        try:
            return (date.fromisoformat(retires_on) - self.today).days
        except ValueError:
            return None

    def replacement(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Newest same-family model on the same provider that is not retiring soon."""
        version = _version(row["model_id"], row["_key"])
        candidates = [
            c
            for c in self.by_family[(row["provider"], _family(row["_key"]))]
            if c["_key"] != row["_key"]
            and _version(c["model_id"], c["_key"]) > version
            and (not c.get("retires_on") or c["retires_on"] > self.window_end)
        ]
        if not candidates:
            return None
        geo = _geo(row["model_id"])
        best = max(
            candidates,
            key=lambda c: (
                _version(c["model_id"], c["_key"]),
                _geo(c["model_id"]) == geo,
                -len(c["model_id"]),
            ),
        )
        # Compare like for like: the retiring model in the replacement's geography.
        same_geo = [
            c
            for c in self.by_key[row["_key"]]
            if c["provider"] == row["provider"] and _geo(c["model_id"]) == _geo(best["model_id"])
        ]
        old = _blend(min(same_geo, key=lambda c: len(c["model_id"])) if same_geo else row)
        return {
            "model_id": best["model_id"],
            "retires_on": best.get("retires_on"),
            "price_change_pct": round((_blend(best) / old - 1) * 100, 1) if old else None,
        }

    def lookup(self, model: str, providers: tuple[str, ...] = ()) -> dict[str, Any] | None:
        """Retirement of a model named on a bill or in config; None if no date is known."""
        key = model_key(model)
        rows = self.by_key.get(key) or self.by_key.get(_plain(key), [])
        if providers:
            rows = [r for r in rows if r["provider"] in providers]
        exact = [r for r in rows if r["model_id"] == model]
        dated = [r for r in exact or rows if r.get("retires_on")]
        if not dated:
            return None
        row = min(dated, key=lambda r: (r["retires_on"], len(r["model_id"])))
        days = self.days_left(row["retires_on"])
        return {
            "model_id": row["model_id"],
            "provider": row["provider"],
            "retires_on": row["retires_on"],
            "days_left": days,
            "status": "retired" if days is not None and days < 0 else "retiring",
            "replacement": self.replacement(row),
        }


def model_retirements(
    within_days: int = WINDOW_DAYS,
    provider: str | None = None,
    model: str | None = None,
    top_n: int = 50,
    *,
    models: list[dict[str, Any]] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """`models` and `today` default to the bundled catalogue and today's date."""
    if not 1 <= within_days <= 1095:
        raise ValueError("within_days must be between 1 and 1095")
    if not 1 <= top_n <= 200:
        raise ValueError("top_n must be between 1 and 200")
    radar = Radar(models, today, within_days)
    want = provider.strip().lower() if provider else None
    filter_key = model_key(model) if model else None

    groups: dict[tuple[str, frozenset[str], str], list[dict[str, Any]]] = defaultdict(list)
    for r in radar.rows:
        retires = r.get("retires_on")
        if not retires:
            continue
        days = radar.days_left(retires)
        if days is None or not -RECENTLY_RETIRED_DAYS <= days <= within_days:
            continue
        if want and want not in (r["provider"].lower(), str(r.get("provider_raw", "")).lower()):
            continue
        if filter_key and not _plain(filter_key) <= r["_key"]:
            continue
        groups[(r["provider"], r["_key"], retires)].append(r)

    entries = []
    for (prov, _, retires), rows in groups.items():
        plain = [r for r in rows if not _GEO_ID.search(r["model_id"])] or rows
        row = min(plain, key=lambda r: (len(r["model_id"]), r["model_id"]))
        days = radar.days_left(retires)
        entries.append(
            {
                "model_id": row["model_id"],
                "provider": prov,
                "retires_on": retires,
                "days_left": days,
                "status": "retired" if days is not None and days < 0 else "retiring",
                "regional_variants": len(rows),
                "replacement": radar.replacement(row),
            }
        )
    entries.sort(key=lambda e: (e["retires_on"], e["provider"], e["model_id"]))
    by_provider = Counter(e["provider"] for e in entries)
    catalogue = _load_catalog() if models is None else {}
    result = {
        "kind": "model_retirements",
        "today": radar.today.isoformat(),
        "window_days": within_days,
        "catalogue_as_of": catalogue.get("as_of"),
        "count": len(entries),
        "by_provider": dict(by_provider.most_common()),
        "models": entries[:top_n],
        "honest_gaps": [
            "Dates come from LiteLLM's community-maintained feed, refreshed weekly, not "
            "from the providers' APIs. Check the provider's own retirement page before "
            "planning a migration.",
            "A model with no date is not listed. No date means none is recorded, not that "
            "the model is safe.",
            "The replacement is the newest model of the same name family on the same "
            "provider that is not retiring in the window. Check quality, context window "
            "and regional availability before switching.",
            "price_change_pct compares list prices at 3 input tokens to 1 output token.",
            f"Models retired in the last {RECENTLY_RETIRED_DAYS} days are kept in the "
            "list, since anything still calling them is already failing.",
        ],
    }
    result["headline"] = _headline(result)
    return result


def _headline(r: dict[str, Any]) -> str:
    if not r["count"]:
        return f"No model in the catalogue retires in the next {r['window_days']} days."
    providers = ", ".join(f"{p} {n}" for p, n in list(r["by_provider"].items())[:5])
    line = (
        f"{r['count']} models retire within {r['window_days']} days or retired recently "
        f"({providers})."
    )
    first = next((e for e in r["models"] if e["status"] == "retiring"), r["models"][0])
    line += f" Next: {first['model_id']} on {first['provider']}, {first['retires_on']}"
    swap = first["replacement"]
    if swap:
        change = swap["price_change_pct"]
        price = f", {change:+.0f}% list price" if change is not None else ""
        line += f"; newest same-family model: {swap['model_id']}{price}"
    return line + "."
