"""Weekly archive of LLM token prices, and what changed between weeks.

Cloud list prices barely move: across April-November 2026 the archive shows
no AWS or Azure change at all. LLM prices move every week - new models,
price cuts, providers added - but the two big catalogs (LiteLLM, ~3,200
model/provider rows; OpenRouter, ~430) were overwritten by each refresh, so
that history was thrown away. This module keeps it.

Each snapshot is `data/llm_prices/YYYY-MM-DD.json.gz`: prices only, keyed by
the source's own model ID, gzipped (~40 KB a week rather than 1.4 MB), and
written byte-for-byte reproducibly so an unchanged week is no diff in git.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_DATA_PACKAGE = "cloudprice_mcp.data"
_DIR = "llm_prices"
_SUFFIX = ".json.gz"

# One row per model ID: [provider, input $/1M, output $/1M, cache-read $/1M].
Row = list[Any]


def compact(litellm: dict[str, Any], openrouter: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce the two full catalogs to the prices worth keeping each week."""
    as_of = litellm["as_of"]
    out: dict[str, Any] = {"as_of": as_of, "schema_version": SCHEMA_VERSION, "sources": {}}
    out["sources"]["litellm"] = {
        m["litellm_id"]: _row(m, m.get("provider", "")) for m in litellm["models"]
    }
    if openrouter is not None:
        out["sources"]["openrouter"] = {
            m["openrouter_id"]: _row(m, m.get("provider_underlying", ""))
            for m in openrouter["models"]
        }
    return out


def _row(m: dict[str, Any], provider: str) -> Row:
    return [
        provider,
        m.get("input_per_1m_usd"),
        m.get("output_per_1m_usd"),
        m.get("cache_read_per_1m_usd"),
    ]


def write_snapshot(snapshot: dict[str, Any], directory: Path) -> Path:
    """Write one snapshot. mtime=0 and sorted keys make the bytes depend only
    on the prices, so re-running a week produces no git change."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snapshot['as_of']}{_SUFFIX}"
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
    return path


def list_dates() -> list[str]:
    """Every archived week, oldest first."""
    root = resources.files(_DATA_PACKAGE).joinpath(_DIR)
    if not root.is_dir():
        return []
    dates = []
    for entry in root.iterdir():
        stem = entry.name.removesuffix(_SUFFIX)
        if entry.name.endswith(_SUFFIX) and _is_iso_date(stem):
            dates.append(stem)
    return sorted(dates)


def load(as_of: str) -> dict[str, Any]:
    """One archived week. Raises FileNotFoundError for a date not in the archive."""
    data = resources.files(_DATA_PACKAGE).joinpath(_DIR, f"{as_of}{_SUFFIX}").read_bytes()
    return json.loads(gzip.decompress(data))


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What changed between two snapshots, per source.

    A price change is any input or output price that differs; cache-read
    changes alone are ignored (few providers publish them consistently, so
    they flicker). Changes are sorted by the largest absolute % move.
    """
    result: dict[str, Any] = {"from": old["as_of"], "to": new["as_of"], "sources": {}}
    for source, new_rows in new["sources"].items():
        old_rows = old["sources"].get(source)
        if old_rows is None:
            continue
        added = sorted(set(new_rows) - set(old_rows))
        removed = sorted(set(old_rows) - set(new_rows))
        changed = []
        for model_id in set(new_rows) & set(old_rows):
            o, n = old_rows[model_id], new_rows[model_id]
            moves = {
                kind: _pct(o[i], n[i])
                for kind, i in (("input", 1), ("output", 2))
                if o[i] != n[i] and o[i] is not None and n[i] is not None
            }
            if moves:
                changed.append(
                    {
                        "model_id": model_id,
                        "provider": n[0],
                        "input_per_1m_usd": [o[1], n[1]],
                        "output_per_1m_usd": [o[2], n[2]],
                        "change_pct": moves,
                    }
                )
        changed.sort(key=lambda c: max(abs(v) for v in c["change_pct"].values()), reverse=True)
        result["sources"][source] = {
            "added": [{"model_id": m, "provider": new_rows[m][0]} for m in added],
            "removed": [{"model_id": m, "provider": old_rows[m][0]} for m in removed],
            "price_changes": changed,
        }
    return result


def _pct(old: float, new: float) -> float:
    if old == 0:
        return 0.0 if new == 0 else 100.0
    return round((new - old) / old * 100, 2)


def _is_iso_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def price_changes(
    *,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    provider: str | None = None,
    query: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """What changed in LLM pricing between two archived weeks.

    `until` defaults to the latest week; `since` to the week before it. Each
    picks the latest archived week on or before that date. `provider` and
    `query` are case-insensitive substrings of the provider and model ID.
    """
    dates = list_dates()
    if len(dates) < 2:
        raise ValueError("the archive needs at least two weeks to compare")
    to = _on_or_before(dates, until) if until else dates[-1]
    frm = _on_or_before(dates, since) if since else dates[dates.index(to) - 1]
    if frm is None or to is None:
        raise ValueError(f"no archived week on or before that date; archive starts {dates[0]}")
    if frm >= to:
        raise ValueError(f"'since' ({frm}) must fall before 'until' ({to})")

    d = diff(load(frm), load(to))
    p = (provider or "").lower()
    q = (query or "").lower()

    def keep(row: dict[str, Any]) -> bool:
        return p in row["provider"].lower() and q in row["model_id"].lower()

    sources: dict[str, Any] = {}
    for name, block in d["sources"].items():
        if source and name != source:
            continue
        lists = {k: [r for r in block[k] if keep(r)] for k in ("added", "removed", "price_changes")}
        sources[name] = {
            "counts": {k: len(v) for k, v in lists.items()},
            "price_changes": lists["price_changes"][:limit],
            "added": lists["added"][:limit],
            "removed": lists["removed"][:limit],
        }

    return {
        "kind": "llm_price_changes",
        "from": frm,
        "to": to,
        "archive": {"first_week": dates[0], "latest_week": dates[-1], "weeks": len(dates)},
        "headline": _headline(frm, to, sources),
        "sources": sources,
        "honest_gaps": [
            "LiteLLM rows are community-maintained: a 'price change' can be a "
            "correction of their data rather than a provider repricing. Check the "
            "provider's pricing page before acting on one.",
            "OpenRouter prices are its routed rate, including its margin, not the "
            "provider's direct price.",
            "Alias IDs ('...-latest', '~...') are repointed to newer models, so their "
            "'price change' is usually a different model, not a repricing.",
            "Weekly granularity: a price that changed and changed back within a "
            "week is not seen. The archive starts 2026-05-23, with a gap until "
            "2026-09-06.",
        ],
    }


def _on_or_before(dates: list[str], day: str) -> str | None:
    earlier = [d for d in dates if d <= day]
    return earlier[-1] if earlier else None


def _headline(frm: str, to: str, sources: dict[str, Any]) -> str:
    parts = []
    for name, block in sources.items():
        c = block["counts"]
        parts.append(
            f"{name}: {c['price_changes']} repriced, {c['added']} added, {c['removed']} removed"
        )
    top = next(
        (b["price_changes"][0] for b in sources.values() if b["price_changes"]),
        None,
    )
    line = f"LLM pricing {frm} -> {to}. " + "; ".join(parts) + "."
    if top:
        move = max(top["change_pct"].values(), key=abs)
        line += f" Biggest move: {top['model_id']} ({move:+.0f}%)."
    return line
