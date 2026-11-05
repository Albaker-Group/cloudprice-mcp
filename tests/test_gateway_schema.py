"""gateway_lambda/tool-schema.json must match the public tools the server exposes.

The hosted chat sees only the tools in that file, and the Cedar allow-list in
infra/policy.tf is generated from it. It once sat at 25 tools for two months
while the server grew to 30, and nothing noticed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from cloudprice_mcp.dispatch import LOCAL_ONLY_TOOLS, tool_names

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "gateway_lambda" / "tool-schema.json"

_spec = importlib.util.spec_from_file_location(
    "export_tool_schema", ROOT / "gateway_lambda" / "export_tool_schema.py"
)
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)


def _committed() -> list[dict]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def test_gateway_schema_is_current():
    # Compared as data, not bytes: the exporter writes CRLF on Windows.
    assert _committed() == export.build_schema(), (
        "Run: uv run python gateway_lambda/export_tool_schema.py"
    )


def test_gateway_schema_lists_exactly_the_public_tools():
    assert sorted(t["name"] for t in _committed()) == tool_names()


def test_gateway_schema_never_offers_a_local_only_tool():
    # These read files on the host. On the gateway that would be the server's disk.
    assert not {t["name"] for t in _committed()} & LOCAL_ONLY_TOOLS
