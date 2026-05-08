"""`cloudprice-mcp fix-path` — Windows PATH repair for pip-installed shim.

Opt-in command. Does NOT run automatically as part of `setup` — it's a separate
concern (user shell PATH vs MCP client config). The default `cloudprice-mcp setup`
flow continues to work without any PATH changes, because the configs it writes
use absolute Python path + `-m` form and don't depend on the shim being on PATH.

This command exists for users who want the bare `cloudprice-mcp` command to work
in their shell as well.
"""
from __future__ import annotations

import argparse
import sys

from . import path_fix


def run_fix_path(args: argparse.Namespace) -> int:
    scripts_dir = path_fix.get_scripts_dir()

    print("🔧 cloudprice-mcp fix-path\n")
    print(f"  Python interpreter:   {sys.executable}")
    print(f"  Scripts directory:    {scripts_dir}")
    shim_status = "yes" if path_fix.shim_exists() else "NO — `pip install cloudprice-mcp` first"
    print(f"  Shim exists:          {shim_status}")
    print(f"  On current PATH:      {'yes' if path_fix.is_on_current_path(scripts_dir) else 'no'}")
    if sys.platform == "win32":
        print(f"  In persistent PATH:   {'yes' if path_fix.is_in_user_path(scripts_dir) else 'no'}")
    print()

    if sys.platform != "win32":
        print(
            "ℹ fix-path only modifies PATH on Windows.\n"
            "  On macOS / Linux, pip's bin directory is typically already on PATH "
            "(/usr/local/bin or ~/.local/bin)."
        )
        if not path_fix.is_on_current_path(scripts_dir):
            print(
                f"\n  If `cloudprice-mcp` doesn't resolve, add {scripts_dir} to your shell rc "
                f"manually:\n    echo 'export PATH=\"{scripts_dir}:$PATH\"' >> ~/.zshrc"
            )
        return 0

    if args.check:
        if path_fix.is_in_user_path(scripts_dir):
            print("✓ Scripts folder is in your persistent user PATH.")
            print("  If `cloudprice-mcp` still doesn't resolve in this shell, open a fresh PowerShell window.")
            return 0
        print("⚠ Scripts folder is NOT in your persistent user PATH.")
        print("  Run `cloudprice-mcp fix-path` (without --check) to add it.")
        print("  Or keep using `python -m cloudprice_mcp.cli ...` — that always works.")
        return 1

    if args.remove:
        removed = path_fix.remove_from_user_path(scripts_dir)
        if removed:
            print(f"✓ Removed {scripts_dir} from user PATH.")
            print("  Open a fresh PowerShell window for the change to take effect.")
        else:
            print(f"○ {scripts_dir} was not in user PATH (nothing to remove).")
        return 0

    # Default mode: add the Scripts directory to user PATH
    if path_fix.is_in_user_path(scripts_dir):
        print("○ Scripts folder is already in your persistent user PATH.")
        print("  If `cloudprice-mcp` still doesn't resolve, open a FRESH PowerShell window.")
        print("  (Existing shells don't refresh PATH — that's a Windows limitation.)")
        return 0

    if not args.yes:
        try:
            answer = input(f"Append '{scripts_dir}' to your user PATH? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if answer not in ("", "y", "yes"):
            print("Aborted. No changes made.")
            return 1

    added = path_fix.add_to_user_path(scripts_dir)
    if added:
        print(f"\n✓ Added {scripts_dir} to user PATH.")
        print("  Open a FRESH PowerShell window for the change to take effect.")
        print("  Then `cloudprice-mcp setup` (and similar) will resolve as bare commands.")
        print("\n  To undo this later: cloudprice-mcp fix-path --remove")
    else:
        print(f"\n○ {scripts_dir} was already in user PATH.")
    return 0


def add_fix_path_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--check", action="store_true",
        help="Inspect-only: report PATH state. Exit 0 if Scripts is on persistent PATH, 1 if not.",
    )
    parser.add_argument(
        "--remove", action="store_true",
        help="Remove the Scripts directory from user PATH (undo a previous fix-path).",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt.",
    )
