"""
`cloudprice-mcp doctor` — diagnose common install / configuration issues.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__ as cloudprice_version
from .setup_cmd import detect_config_path, detect_python_command


CHECK = "✓"
CROSS = "✗"
WARN = "⚠"


def _ok(label: str, detail: str = "") -> None:
    detail_str = f" — {detail}" if detail else ""
    print(f"  {CHECK} {label}{detail_str}")


def _fail(label: str, detail: str) -> None:
    print(f"  {CROSS} {label} — {detail}")


def _warn(label: str, detail: str) -> None:
    print(f"  {WARN} {label} — {detail}")


def _check_python_version() -> bool:
    py = sys.version_info
    if py >= (3, 10):
        _ok("Python version", f"{py.major}.{py.minor}.{py.micro} (3.10+ required)")
        return True
    _fail(
        "Python version",
        f"{py.major}.{py.minor}.{py.micro} — cloudprice-mcp requires 3.10+",
    )
    return False


def _check_package_install() -> bool:
    _ok("cloudprice-mcp package", f"v{cloudprice_version}")
    return True


def _check_tool_registration() -> tuple[bool, int]:
    try:
        from .server import list_tools
        tools = asyncio.run(list_tools())
        _ok("MCP tools registered", f"{len(tools)} tools")
        return True, len(tools)
    except Exception as e:  # pragma: no cover
        _fail("MCP tools registered", f"{type(e).__name__}: {e}")
        return False, 0


def _check_config_file() -> tuple[bool, Path | None, dict | None]:
    config_path, install_kind = detect_config_path()
    print(f"  {CHECK} Detected platform — {install_kind}")
    print(f"  {CHECK} Expected Claude Desktop config path — {config_path}")
    if not config_path.exists():
        _fail(
            "Config file",
            f"NOT FOUND at {config_path}. Run `cloudprice-mcp setup` to create it.",
        )
        return False, config_path, None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _fail("Config file", f"invalid JSON: {e}")
        return False, config_path, None
    _ok("Config file", "exists and parses as JSON")
    return True, config_path, config


def _check_cloudprice_entry(config: dict) -> bool:
    mcp_servers = config.get("mcpServers", {}) or {}
    if "cloudprice" not in mcp_servers:
        _fail(
            "Cloudprice entry in config",
            "missing — Claude Desktop won't know about us. Run `cloudprice-mcp setup`.",
        )
        return False
    _ok("Cloudprice entry in config", "present")
    return True


def _check_command_path(config: dict) -> bool:
    server = config.get("mcpServers", {}).get("cloudprice", {})
    cmd = server.get("command")
    if not cmd:
        _fail("Command field", "missing in cloudprice entry")
        return False
    cmd_path = Path(cmd)
    if cmd_path.is_absolute() and not cmd_path.exists():
        _warn(
            "Command path",
            f"absolute path '{cmd}' does NOT exist on this system. "
            "Re-run `cloudprice-mcp setup` to refresh.",
        )
        return False
    expected_python = detect_python_command()
    if cmd_path.is_absolute() and str(cmd_path) != expected_python:
        _warn(
            "Command field",
            f"points at '{cmd}' but current Python is '{expected_python}'. "
            "Different env may serve different cloudprice-mcp versions.",
        )
        return True  # not strictly broken, just suspicious
    _ok("Command field", f"'{cmd}'")
    return True


def _check_args_field(config: dict) -> bool:
    server = config.get("mcpServers", {}).get("cloudprice", {})
    args = server.get("args", [])
    if args == ["-m", "cloudprice_mcp.server"]:
        _ok("Args field", "['-m', 'cloudprice_mcp.server'] (recommended)")
        return True
    if args == []:
        _ok("Args field", "[] (using cloudprice-mcp shim — works if shim is on PATH)")
        return True
    _warn(
        "Args field",
        f"non-standard: {args}. Should be ['-m', 'cloudprice_mcp.server'] or [].",
    )
    return True


def run_doctor(_args: argparse.Namespace) -> int:
    print("🩺 cloudprice-mcp doctor — running checks\n")

    print("Install:")
    py_ok = _check_python_version()
    pkg_ok = _check_package_install()
    tools_ok, _tool_count = _check_tool_registration()

    print("\nClaude Desktop config:")
    cfg_ok, cfg_path, config = _check_config_file()
    entry_ok = _check_cloudprice_entry(config) if cfg_ok and config else False
    cmd_ok = _check_command_path(config) if entry_ok and config else False
    args_ok = _check_args_field(config) if entry_ok and config else False

    print()
    all_ok = py_ok and pkg_ok and tools_ok and cfg_ok and entry_ok and cmd_ok and args_ok
    if all_ok:
        print("✅ All checks passed.")
        print(
            "   If Claude Desktop still doesn't show the connector, fully quit it "
            "(Cmd+Q on Mac, right-click tray → Quit on Windows) and reopen.\n"
            "   You can also try: cloudprice-mcp setup --yes  (rewrites config + kills cached subprocess)."
        )
        return 0
    print("⚠ Some checks failed. See details above.")
    print("   Quick fix attempt: cloudprice-mcp setup --yes")
    return 1
