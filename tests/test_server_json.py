"""server.json is what the official MCP Registry publishes; keep it in step.

The registry rejects a description over 100 characters, and verifies PyPI
ownership by finding `mcp-name: <name>` in the README. A mismatch on either
only shows up as a failed release, after PyPI has already been published.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def test_versions_match_pyproject():
    # A regex, not tomllib: CI still runs Python 3.10, which has no tomllib.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M)[1]
    assert SERVER["version"] == version
    assert all(p["version"] == version for p in SERVER["packages"])


def test_readme_carries_the_registry_name():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"<!-- mcp-name: {SERVER['name']} -->" in readme


def test_description_fits_the_registry_limit():
    assert 0 < len(SERVER["description"]) <= 100
