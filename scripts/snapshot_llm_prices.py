"""Archive this week's LLM catalogs as data/llm_prices/<as_of>.json.gz.

Run by the weekly refresh after the LiteLLM and OpenRouter catalogs are
fetched. Safe to re-run: the output is reproducible, so an unchanged week
writes identical bytes.

    uv run python scripts/snapshot_llm_prices.py
"""

from __future__ import annotations

import json
from pathlib import Path

from cloudprice_mcp.llm_history import compact, write_snapshot

DATA = Path(__file__).resolve().parents[1] / "src" / "cloudprice_mcp" / "data"


def main() -> None:
    litellm = json.loads((DATA / "llm_catalog_extended.json").read_text(encoding="utf-8"))
    openrouter_path = DATA / "llm_catalog_openrouter.json"
    openrouter = (
        json.loads(openrouter_path.read_text(encoding="utf-8"))
        if openrouter_path.exists()
        else None
    )
    path = write_snapshot(compact(litellm, openrouter), DATA / "llm_prices")
    print(f"wrote {path.name} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
