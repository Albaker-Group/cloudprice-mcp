"""
`cloudprice-mcp setup` command — auto-configure Claude Desktop on Windows / macOS / Linux.

Trust spectrum:
  cloudprice-mcp setup                 # interactive (shows config, asks Y/N)
  cloudprice-mcp setup --yes           # skip prompt, just do it
  cloudprice-mcp setup --dry-run       # show what would change, don't write
  cloudprice-mcp setup --print-config  # output the JSON to stdout for manual paste
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable


def _windows_config_paths() -> list[Path]:
    """Return possible Claude Desktop config paths on Windows, most-specific first."""
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    paths: list[Path] = []
    # Microsoft Store install (sandboxed) — try first because it's more common now
    if localappdata:
        paths.append(
            Path(localappdata)
            / "Packages"
            / "Claude_pzs8sxrjxfjjc"
            / "LocalCache"
            / "Roaming"
            / "Claude"
            / "claude_desktop_config.json"
        )
    # Direct .exe install
    if appdata:
        paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    return paths


def _mac_config_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json"
    )


def _linux_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "Claude" / "claude_desktop_config.json"


def detect_config_path() -> tuple[Path, str]:
    """Return (path, install_kind_label). Prefers existing files; falls back to default."""
    if sys.platform == "win32":
        candidates = _windows_config_paths()
        for path in candidates:
            if path.exists():
                kind = "Microsoft Store" if "Packages" in str(path) else "direct .exe"
                return path, f"Windows ({kind})"
        # Neither exists yet — default to MS Store path (more common in 2026)
        return candidates[0], "Windows (MS Store, config dir not yet created)"
    if sys.platform == "darwin":
        return _mac_config_path(), "macOS"
    return _linux_config_path(), "Linux"


def detect_python_command() -> str:
    """Return the absolute path to the current Python interpreter.

    Why absolute: macOS Claude Desktop launches with a minimal PATH that often
    doesn't include where `python3` lives. Using sys.executable is bulletproof.
    """
    return sys.executable


def build_cloudprice_entry() -> dict:
    """The MCP server entry to merge into claude_desktop_config.json."""
    return {
        "command": detect_python_command(),
        "args": ["-m", "cloudprice_mcp.server"],
    }


def merge_config(existing: dict | None, cloudprice_entry: dict) -> dict:
    """Merge cloudprice into existing config, preserving everything else."""
    config = dict(existing) if existing else {}
    mcp_servers = dict(config.get("mcpServers") or {})
    mcp_servers["cloudprice"] = cloudprice_entry
    config["mcpServers"] = mcp_servers
    return config


def read_existing_config(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(
            f"⚠️  Existing config at {path} is not valid JSON: {e}",
            file=sys.stderr,
        )
        print("    Refusing to overwrite. Fix manually or back it up first.", file=sys.stderr)
        sys.exit(2)


def kill_cached_subprocesses() -> int:
    """Kill any lingering cloudprice-mcp subprocesses so Claude Desktop respawns fresh."""
    killed = 0
    if sys.platform == "win32":
        # PowerShell-based kill
        try:
            cmd = (
                "Get-Process | Where-Object { $_.Path -like \"*cloudprice-mcp*\" } "
                "| ForEach-Object { Stop-Process -Id $_.Id -Force; 1 }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            killed = len([line for line in result.stdout.splitlines() if line.strip() == "1"])
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    else:
        # macOS / Linux: pkill
        for pattern in ("cloudprice-mcp", "cloudprice_mcp"):
            try:
                result = subprocess.run(
                    ["pkill", "-f", pattern], capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    killed += 1
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
    return killed


def restart_instructions(install_kind: str) -> str:
    if "Windows" in install_kind:
        return (
            "1. Right-click Claude Desktop in the system tray (bottom-right of taskbar) → Quit\n"
            "   (For Microsoft Store install: also try File → Exit inside Claude window if tray Quit doesn't work.)\n"
            "2. Wait 5 seconds.\n"
            "3. Reopen Claude Desktop from Start Menu.\n"
            "4. Click the + button in the chat composer → Connectors → cloudprice should appear with 9 tools."
        )
    if "macOS" in install_kind:
        return (
            "1. In Claude Desktop, press Cmd+Q to fully quit (NOT just close the window).\n"
            "2. Wait 5 seconds.\n"
            "3. Reopen from Applications or Spotlight (Cmd+Space → 'Claude').\n"
            "4. Click the + button in the chat composer → Connectors → cloudprice should appear with 9 tools."
        )
    return (
        "1. Quit Claude Desktop fully.\n"
        "2. Wait 5 seconds.\n"
        "3. Reopen Claude Desktop.\n"
        "4. Click the + button → Connectors → cloudprice should appear with 9 tools."
    )


def _print_section(title: str, lines: Iterable[str] = ()) -> None:
    print(f"\n  {title}")
    for line in lines:
        print(f"    {line}")


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("", "y", "yes")


def run_setup(args: argparse.Namespace) -> int:
    """Top-level handler for `cloudprice-mcp setup`."""
    config_path, install_kind = detect_config_path()
    python_cmd = detect_python_command()
    cloudprice_entry = build_cloudprice_entry()
    existing = read_existing_config(config_path)
    merged = merge_config(existing, cloudprice_entry)
    merged_json = json.dumps(merged, indent=2)

    if args.print_config:
        # Just print the JSON. No detection chatter, no prompts. Pure paste-ability.
        print(merged_json)
        return 0

    print("🔍 cloudprice-mcp setup")
    _print_section(f"Platform detected: {install_kind}")
    _print_section(f"Python interpreter (will be used in config): {python_cmd}")
    _print_section(f"Claude Desktop config path: {config_path}")
    if existing is None:
        _print_section("Existing config: NOT FOUND — a new file will be created")
    else:
        existing_keys = list(existing.keys())
        _print_section(
            f"Existing config: FOUND ({len(existing_keys)} top-level keys: "
            f"{', '.join(existing_keys) or '<empty>'})"
        )
        _print_section("All existing keys will be preserved.")

    print("\n📝 Config that will be written:")
    for line in merged_json.splitlines():
        print(f"    {line}")

    if args.dry_run:
        print("\n--dry-run set: no file written.")
        return 0

    if not args.yes and not _confirm("\nProceed?"):
        print("Aborted. No changes made.")
        return 1

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(merged_json + "\n", encoding="utf-8")
    print(f"\n✅ Wrote config to {config_path}")

    killed = kill_cached_subprocesses()
    if killed:
        print(f"✅ Killed {killed} cached cloudprice-mcp subprocess(es).")
    else:
        print("✅ No cached cloudprice-mcp subprocesses found (clean slate).")

    print("\n🎯 Now restart Claude Desktop:\n")
    print(restart_instructions(install_kind))
    print("\n💡 Run `cloudprice-mcp doctor` if anything looks wrong.")
    return 0
