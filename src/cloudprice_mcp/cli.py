"""
Top-level CLI dispatcher for `cloudprice-mcp`.

Default (no args) — runs the MCP server over stdio (backward-compatible behavior).
Subcommands:
  cloudprice-mcp serve    explicit alias for default
  cloudprice-mcp setup    auto-configure Claude Desktop
  cloudprice-mcp doctor   diagnose install / config issues
"""
from __future__ import annotations

import argparse
import sys

from . import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudprice-mcp",
        description=(
            "MCP server that compares cloud pricing across AWS, Azure, GCP, and OCI."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"cloudprice-mcp {__version__}",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    sub.add_parser(
        "serve",
        help="Run the MCP server over stdio (default if no command given).",
    )

    p_setup = sub.add_parser(
        "setup",
        help="Auto-configure Claude Desktop on Windows / macOS / Linux.",
    )
    p_setup.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the interactive Y/N confirmation.",
    )
    p_setup.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be written without modifying any files.",
    )
    p_setup.add_argument(
        "--print-config", action="store_true",
        help="Print only the merged config JSON to stdout, for users who prefer to paste it manually.",
    )

    sub.add_parser(
        "doctor",
        help="Diagnose install + Claude Desktop config issues.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout/stderr on Windows so the emoji-laden output renders
    # instead of crashing on cp1252. Safe no-op on macOS / Linux.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd in (None, "serve"):
        # Default: run the MCP server (preserves the v0.3.x entry-point behavior).
        from .server import main as serve_main
        serve_main()
        return 0
    if args.cmd == "setup":
        from .setup_cmd import run_setup
        return run_setup(args)
    if args.cmd == "doctor":
        from .doctor_cmd import run_doctor
        return run_doctor(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
