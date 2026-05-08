"""
Per-client adapters for MCP server registration.

Each adapter encapsulates one AI client's quirks:
  - where its MCP config lives on each OS
  - the schema variant (mcpServers vs servers vs context_servers vs per-file)
  - how to merge a cloudprice entry without breaking other entries
  - how to detect whether the client is installed
  - restart + verification hints

Tier 1 (verified, smoke-tested): Claude Desktop, GitHub Copilot
Tier 2 (documented, format confirmed): Cursor, Windsurf, Cline
Tier 3 (best-effort, may need manual tweak): Continue, Zed
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ENTRY_NAME = "cloudprice"
CLAUDE_CONFIG_FILE = "claude_desktop_config.json"
MCP_CONFIG_FILE = "mcp.json"
_VS_CODE_USER_DIR = "Code"
_VS_CODE_USER_SUBPATH = "User"


def _appdata() -> Path:
    """Windows %APPDATA% (Roaming) — empty Path if env var unset (non-Windows)."""
    return Path(os.environ.get("APPDATA", ""))


def _local_appdata() -> Path:
    """Windows %LOCALAPPDATA% — empty Path if env var unset (non-Windows)."""
    return Path(os.environ.get("LOCALAPPDATA", ""))


def _mac_app_support() -> Path:
    """~/Library/Application Support — macOS standard config location."""
    return Path.home() / "Library" / "Application Support"


def _xdg_config_home() -> Path:
    """$XDG_CONFIG_HOME if set, else ~/.config — Linux/XDG standard."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def _vscode_user_dir() -> Path:
    """VS Code's user-data directory: %APPDATA%/Code/User on Windows,
    ~/Library/Application Support/Code/User on macOS, ~/.config/Code/User on Linux."""
    if sys.platform == "win32":
        return _appdata() / _VS_CODE_USER_DIR / _VS_CODE_USER_SUBPATH
    if sys.platform == "darwin":
        return _mac_app_support() / _VS_CODE_USER_DIR / _VS_CODE_USER_SUBPATH
    return _xdg_config_home() / _VS_CODE_USER_DIR / _VS_CODE_USER_SUBPATH


@dataclass
class WriteResult:
    client_name: str
    display_name: str
    config_path: Path
    action: str  # see ACTIONS below
    config: dict


# Action constants
ACTION_WROTE_NEW = "wrote_new_file"
ACTION_MERGED = "merged_into_existing"
ACTION_REFRESHED = "refreshed_existing_entry"
ACTION_SKIPPED_IDENTICAL = "skipped_identical"
ACTION_WOULD_WRITE_NEW = "would_write_new_file"
ACTION_WOULD_MERGE = "would_merge"
ACTION_WOULD_REFRESH = "would_refresh"
ACTION_WOULD_SKIP_IDENTICAL = "would_skip_identical"


class ClientAdapter:
    """Base adapter. Subclasses override config_path() and merge_entry()."""

    name: str = ""
    display_name: str = ""

    def config_path(self) -> Path:
        raise NotImplementedError

    def is_installed(self) -> bool:
        """Heuristic: parent directory of the config file exists."""
        try:
            return self.config_path().parent.exists()
        except (OSError, KeyError, RuntimeError):
            return False

    def read_existing_config(self) -> dict | None:
        path = self.config_path()
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} is not valid JSON: {e}") from e

    def build_entry(self, command: str, args: list[str]) -> dict:
        """The cloudprice entry as it appears under the schema's container."""
        return {"command": command, "args": list(args)}

    def merge_entry(self, existing: dict | None, command: str, args: list[str]) -> dict:
        """Default: flat mcpServers schema. Override for other shapes."""
        config = dict(existing) if existing else {}
        servers = dict(config.get("mcpServers") or {})
        servers[ENTRY_NAME] = self.build_entry(command, args)
        config["mcpServers"] = servers
        return config

    def already_present(self, existing: dict | None) -> bool:
        """Whether cloudprice is registered (regardless of correctness)."""
        if not existing:
            return False
        return ENTRY_NAME in (existing.get("mcpServers") or {})

    def existing_matches(self, existing: dict | None, command: str, args: list[str]) -> bool:
        """Whether the existing entry already matches what we'd write."""
        if not self.already_present(existing):
            return False
        current = (existing.get("mcpServers") or {}).get(ENTRY_NAME) or {}
        target = self.build_entry(command, args)
        return current == target

    def restart_instructions(self) -> str:
        return f"Fully quit {self.display_name}, wait 5s, reopen."

    def verify_hint(self) -> str:
        return f"Open {self.display_name} and check that cloudprice tools appear in the MCP / tools list."

    def plan_action(
        self,
        existing: dict | None,
        command: str,
        args: list[str],
        force: bool,
        dry_run: bool,
    ) -> str:
        if existing is None:
            return ACTION_WOULD_WRITE_NEW if dry_run else ACTION_WROTE_NEW
        if self.existing_matches(existing, command, args) and not force:
            return ACTION_WOULD_SKIP_IDENTICAL if dry_run else ACTION_SKIPPED_IDENTICAL
        if self.already_present(existing):
            return ACTION_WOULD_REFRESH if dry_run else ACTION_REFRESHED
        return ACTION_WOULD_MERGE if dry_run else ACTION_MERGED

    def apply(
        self,
        command: str,
        args: list[str],
        force: bool = False,
        dry_run: bool = False,
    ) -> WriteResult:
        try:
            existing = self.read_existing_config()
        except ValueError as e:
            raise ValueError(f"{self.display_name}: {e}") from e

        action = self.plan_action(existing, command, args, force, dry_run)

        if action == ACTION_SKIPPED_IDENTICAL or action == ACTION_WOULD_SKIP_IDENTICAL:
            return WriteResult(
                self.name, self.display_name, self.config_path(), action, existing or {}
            )

        merged = self.merge_entry(existing, command, args)

        if not dry_run:
            path = self.config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

        return WriteResult(self.name, self.display_name, self.config_path(), action, merged)


# -------- Concrete adapters --------


class ClaudeDesktopAdapter(ClientAdapter):
    name = "claude"
    display_name = "Claude Desktop"

    def config_path(self) -> Path:
        if sys.platform == "win32":
            for p in self._windows_candidates():
                if p.exists():
                    return p
            return self._windows_candidates()[0]
        if sys.platform == "darwin":
            return _mac_app_support() / "Claude" / CLAUDE_CONFIG_FILE
        return _xdg_config_home() / "Claude" / CLAUDE_CONFIG_FILE

    @staticmethod
    def _windows_candidates() -> list[Path]:
        out: list[Path] = []
        if os.environ.get("LOCALAPPDATA"):
            out.append(
                _local_appdata()
                / "Packages"
                / "Claude_pzs8sxrjxfjjc"
                / "LocalCache"
                / "Roaming"
                / "Claude"
                / CLAUDE_CONFIG_FILE
            )
        if os.environ.get("APPDATA"):
            out.append(_appdata() / "Claude" / CLAUDE_CONFIG_FILE)
        return out

    def is_installed(self) -> bool:
        if sys.platform == "win32":
            for p in self._windows_candidates():
                if p.parent.exists():
                    return True
            return False
        return self.config_path().parent.exists()

    def restart_instructions(self) -> str:
        if sys.platform == "win32":
            return (
                "Right-click Claude in the system tray → Quit "
                "(or File → Exit inside the window). Wait 5s, reopen from Start Menu."
            )
        if sys.platform == "darwin":
            return "Press Cmd+Q to fully quit Claude (NOT just close the window). Wait 5s, reopen."
        return "Quit Claude Desktop fully. Wait 5s, reopen."

    def verify_hint(self) -> str:
        return "Click + in chat composer → Connectors → cloudprice should appear with 10 tools."


class CopilotAdapter(ClientAdapter):
    """GitHub Copilot Agent Mode in VS Code. Uses `servers` + `type: stdio` schema."""

    name = "copilot"
    display_name = "GitHub Copilot Agent Mode (VS Code)"

    def config_path(self) -> Path:
        return _vscode_user_dir() / MCP_CONFIG_FILE

    def build_entry(self, command: str, args: list[str]) -> dict:
        return {"type": "stdio", "command": command, "args": list(args)}

    def merge_entry(self, existing: dict | None, command: str, args: list[str]) -> dict:
        config = dict(existing) if existing else {}
        servers = dict(config.get("servers") or {})
        servers[ENTRY_NAME] = self.build_entry(command, args)
        config["servers"] = servers
        return config

    def already_present(self, existing: dict | None) -> bool:
        if not existing:
            return False
        return ENTRY_NAME in (existing.get("servers") or {})

    def existing_matches(self, existing: dict | None, command: str, args: list[str]) -> bool:
        if not self.already_present(existing):
            return False
        current = (existing.get("servers") or {}).get(ENTRY_NAME) or {}
        return current == self.build_entry(command, args)

    def restart_instructions(self) -> str:
        return (
            "Fully quit VS Code (close all windows, then verify Code.exe is gone "
            "from Task Manager / Activity Monitor). Reopen, then open Copilot Chat → switch to Agent mode."
        )

    def verify_hint(self) -> str:
        return (
            "Open Copilot Chat → switch to Agent mode → click the tools/wrench icon — "
            "cloudprice's 10 tools should appear in the list."
        )


class CursorAdapter(ClientAdapter):
    """Cursor uses `mcpServers` + `type: stdio` (same shape as Copilot's entries but different root key)."""

    name = "cursor"
    display_name = "Cursor"

    def config_path(self) -> Path:
        return Path.home() / ".cursor" / MCP_CONFIG_FILE

    def build_entry(self, command: str, args: list[str]) -> dict:
        return {"type": "stdio", "command": command, "args": list(args)}

    def restart_instructions(self) -> str:
        return "Fully quit Cursor (Cmd+Q on macOS, File → Exit on Windows/Linux). Reopen."

    def verify_hint(self) -> str:
        return "Cursor → Settings → MCP → cloudprice should appear; tools become available in Composer / Chat."


class WindsurfAdapter(ClientAdapter):
    """Windsurf (Codeium) uses standard `mcpServers` flat schema."""

    name = "windsurf"
    display_name = "Windsurf (Codeium)"

    def config_path(self) -> Path:
        return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"

    def restart_instructions(self) -> str:
        return "Fully quit Windsurf, reopen, and click the MCPs icon in the Cascade panel."

    def verify_hint(self) -> str:
        return "Windsurf Cascade panel → MCPs icon → cloudprice should be listed."


class ClineAdapter(ClientAdapter):
    """Cline VS Code extension. Stores config in VS Code's globalStorage."""

    name = "cline"
    display_name = "Cline (VS Code extension)"

    EXT_ID = "saoudrizwan.claude-dev"
    SETTINGS_FILE = "cline_mcp_settings.json"

    def config_path(self) -> Path:
        return (
            _vscode_user_dir()
            / "globalStorage"
            / self.EXT_ID
            / "settings"
            / self.SETTINGS_FILE
        )

    def is_installed(self) -> bool:
        # Cline writes globalStorage only after the extension has run at least once.
        # If the directory doesn't exist, the extension probably isn't installed.
        try:
            return self.config_path().parent.parent.exists()
        except (OSError, KeyError, RuntimeError):
            return False

    def restart_instructions(self) -> str:
        return (
            "In VS Code: open Cline panel → Settings (gear) → MCP Servers → Restart, "
            "or fully quit and reopen VS Code."
        )

    def verify_hint(self) -> str:
        return "Cline panel → MCP Servers tab → cloudprice should appear with status 'Connected'."


class ContinueAdapter(ClientAdapter):
    """Continue.dev — writes a per-server JSON file under ~/.continue/mcpServers/."""

    name = "continue"
    display_name = "Continue.dev"

    def config_path(self) -> Path:
        return Path.home() / ".continue" / "mcpServers" / "cloudprice.json"

    def is_installed(self) -> bool:
        return (Path.home() / ".continue").exists()

    def read_existing_config(self) -> dict | None:
        path = self.config_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def build_entry(self, command: str, args: list[str]) -> dict:
        return {"name": ENTRY_NAME, "command": command, "args": list(args)}

    def merge_entry(self, existing: dict | None, command: str, args: list[str]) -> dict:
        # Continue's per-server file IS the entry — there's nothing to merge into.
        # We just overwrite with the new entry.
        return self.build_entry(command, args)

    def already_present(self, existing: dict | None) -> bool:
        return existing is not None and existing.get("name") == ENTRY_NAME

    def existing_matches(self, existing: dict | None, command: str, args: list[str]) -> bool:
        if not existing:
            return False
        return existing == self.build_entry(command, args)

    def restart_instructions(self) -> str:
        return "Reload Continue (VS Code: Cmd+Shift+P → 'Continue: Reload') or fully quit + reopen the editor."

    def verify_hint(self) -> str:
        return "Continue panel → MCP Servers → cloudprice should be listed."


class ZedAdapter(ClientAdapter):
    """Zed editor — uses `context_servers` key in settings.json."""

    name = "zed"
    display_name = "Zed"

    def config_path(self) -> Path:
        if sys.platform == "win32":
            return _appdata() / "Zed" / "settings.json"
        # macOS Zed uses XDG-style ~/.config/zed by default; both Linux and macOS share this.
        return _xdg_config_home() / "zed" / "settings.json"

    def merge_entry(self, existing: dict | None, command: str, args: list[str]) -> dict:
        config = dict(existing) if existing else {}
        servers = dict(config.get("context_servers") or {})
        servers[ENTRY_NAME] = self.build_entry(command, args)
        config["context_servers"] = servers
        return config

    def already_present(self, existing: dict | None) -> bool:
        if not existing:
            return False
        return ENTRY_NAME in (existing.get("context_servers") or {})

    def existing_matches(self, existing: dict | None, command: str, args: list[str]) -> bool:
        if not self.already_present(existing):
            return False
        current = (existing.get("context_servers") or {}).get(ENTRY_NAME) or {}
        return current == self.build_entry(command, args)

    def restart_instructions(self) -> str:
        return "Fully quit Zed and reopen."

    def verify_hint(self) -> str:
        return "Zed Agent Panel → Settings → cloudprice should appear under Context Servers."


# -------- Registry --------

ALL_ADAPTERS: tuple[type[ClientAdapter], ...] = (
    ClaudeDesktopAdapter,
    CopilotAdapter,
    CursorAdapter,
    WindsurfAdapter,
    ClineAdapter,
    ContinueAdapter,
    ZedAdapter,
)


def all_adapters() -> list[ClientAdapter]:
    return [cls() for cls in ALL_ADAPTERS]


def adapter_by_name(name: str) -> ClientAdapter | None:
    for cls in ALL_ADAPTERS:
        if cls.name == name:
            return cls()
    return None


def known_client_names() -> list[str]:
    return [cls.name for cls in ALL_ADAPTERS]


def detect_installed() -> list[ClientAdapter]:
    """Return adapters whose target client appears to be installed on this system."""
    return [a for a in all_adapters() if a.is_installed()]
