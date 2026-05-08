"""
Top-level CLI dispatcher for `cloudprice-mcp`.

Default (no args) — runs the MCP server over stdio (backward-compatible behavior).
Subcommands:
  cloudprice-mcp serve    explicit alias for default
  cloudprice-mcp setup    auto-configure any installed MCP-compatible client
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
            "MCP server that compares cloud pricing across AWS, Azure, GCP, and OCI. "
            "Works with Claude Desktop, GitHub Copilot, Cursor, Windsurf, Cline, Continue, and Zed."
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
        help="Auto-configure cloudprice-mcp in any installed MCP-compatible AI client.",
    )
    # Setup args defined in setup_cmd to keep all flag knowledge in one place.
    from .setup_cmd import add_setup_arguments
    add_setup_arguments(p_setup)

    sub.add_parser(
        "doctor",
        help="Diagnose install + per-client config issues.",
    )

    p_fix_path = sub.add_parser(
        "fix-path",
        help="(Windows) Add Python's Scripts folder to user PATH so `cloudprice-mcp` resolves as a bare shell command. Opt-in.",
    )
    from .fix_path_cmd import add_fix_path_arguments
    add_fix_path_arguments(p_fix_path)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout/stderr on Windows so emoji-laden output doesn't crash on cp1252.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd in (None, "serve"):
        from .server import main as serve_main
        serve_main()
        return 0
    if args.cmd == "setup":
        from .setup_cmd import run_setup
        return run_setup(args)
    if args.cmd == "doctor":
        from .doctor_cmd import run_doctor
        return run_doctor(args)
    if args.cmd == "fix-path":
        from .fix_path_cmd import run_fix_path
        return run_fix_path(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
