"""docs/TOOLS.md and the README's tool count must match what the server exposes."""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path

from cloudprice_mcp.server import list_tools

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("gen_tools_md", ROOT / "scripts" / "gen_tools_md.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_tools_doc_is_current():
    current = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    assert current == gen.render(), "Run: uv run python scripts/gen_tools_md.py"


def test_readme_tool_count_matches_server():
    count = len(asyncio.run(list_tools()))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claimed = {int(n) for n in re.findall(r"\*\*(\d+) tools\*\*", readme)}
    assert claimed == {count}
