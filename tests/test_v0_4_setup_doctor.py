"""Tests for v0.4 cli + setup + doctor commands."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from cloudprice_mcp import setup_cmd
from cloudprice_mcp.setup_cmd import (
    build_cloudprice_entry,
    detect_python_command,
    merge_config,
    read_existing_config,
    run_setup,
)


# --- merge_config ---

def test_merge_into_empty_config():
    entry = {"command": "/bin/python3", "args": ["-m", "cloudprice_mcp.server"]}
    result = merge_config(None, entry)
    assert result == {"mcpServers": {"cloudprice": entry}}


def test_merge_preserves_other_top_level_keys():
    existing = {
        "preferences": {"theme": "dark", "ccdScheduledTasksEnabled": True},
        "telemetry": False,
    }
    entry = {"command": "/bin/python3", "args": ["-m", "cloudprice_mcp.server"]}
    result = merge_config(existing, entry)
    assert result["preferences"] == existing["preferences"]
    assert result["telemetry"] is False
    assert result["mcpServers"]["cloudprice"] == entry


def test_merge_preserves_other_mcp_servers():
    existing = {"mcpServers": {"github": {"command": "node"}}}
    entry = {"command": "/bin/python3", "args": ["-m", "cloudprice_mcp.server"]}
    result = merge_config(existing, entry)
    assert result["mcpServers"]["github"] == {"command": "node"}
    assert result["mcpServers"]["cloudprice"] == entry


def test_merge_overwrites_existing_cloudprice_entry():
    existing = {"mcpServers": {"cloudprice": {"command": "/old/python"}}}
    entry = {"command": "/new/python", "args": ["-m", "cloudprice_mcp.server"]}
    result = merge_config(existing, entry)
    assert result["mcpServers"]["cloudprice"] == entry


# --- detect_python_command ---

def test_detect_python_command_returns_absolute_path():
    cmd = detect_python_command()
    assert Path(cmd).is_absolute()
    # sys.executable always exists for the running interpreter
    assert Path(cmd).exists()


def test_build_cloudprice_entry_shape():
    entry = build_cloudprice_entry()
    assert "command" in entry
    assert "args" in entry
    assert entry["args"] == ["-m", "cloudprice_mcp.server"]
    assert Path(entry["command"]).is_absolute()


# --- read_existing_config ---

def test_read_existing_config_when_missing(tmp_path):
    path = tmp_path / "nonexistent.json"
    assert read_existing_config(path) is None


def test_read_existing_config_valid(tmp_path):
    path = tmp_path / "config.json"
    payload = {"preferences": {"theme": "dark"}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_existing_config(path) == payload


def test_read_existing_config_invalid_json_aborts(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        read_existing_config(path)
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "not valid JSON" in captured.err


# --- run_setup integration ---

def _make_args(**overrides) -> argparse.Namespace:
    base = {"yes": False, "dry_run": False, "print_config": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_run_setup_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    target = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(setup_cmd, "detect_config_path", lambda: (target, "TestOS"))
    rc = run_setup(_make_args(dry_run=True))
    assert rc == 0
    assert not target.exists(), "dry-run must not create the file"
    captured = capsys.readouterr()
    assert "no file written" in captured.out.lower()


def test_run_setup_print_config_emits_only_json(tmp_path, monkeypatch, capsys):
    target = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(setup_cmd, "detect_config_path", lambda: (target, "TestOS"))
    rc = run_setup(_make_args(print_config=True))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)  # Must be valid, parseable JSON only
    assert "mcpServers" in parsed
    assert "cloudprice" in parsed["mcpServers"]


def test_run_setup_yes_writes_config(tmp_path, monkeypatch, capsys):
    target = tmp_path / "subdir" / "claude_desktop_config.json"
    monkeypatch.setattr(setup_cmd, "detect_config_path", lambda: (target, "TestOS"))
    monkeypatch.setattr(setup_cmd, "kill_cached_subprocesses", lambda: 0)
    rc = run_setup(_make_args(yes=True))
    assert rc == 0
    assert target.exists()
    written = json.loads(target.read_text(encoding="utf-8"))
    assert "cloudprice" in written["mcpServers"]


def test_run_setup_yes_preserves_existing_keys(tmp_path, monkeypatch):
    target = tmp_path / "claude_desktop_config.json"
    target.write_text(
        json.dumps({"preferences": {"theme": "dark"}, "telemetry": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_cmd, "detect_config_path", lambda: (target, "TestOS"))
    monkeypatch.setattr(setup_cmd, "kill_cached_subprocesses", lambda: 0)
    run_setup(_make_args(yes=True))
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["preferences"] == {"theme": "dark"}
    assert written["telemetry"] is False
    assert "cloudprice" in written["mcpServers"]


# --- detect_config_path platform behavior ---

def test_detect_config_path_returns_absolute_path():
    path, _kind = setup_cmd.detect_config_path()
    assert path.is_absolute()


def test_detect_config_path_label_matches_platform():
    _, kind = setup_cmd.detect_config_path()
    if sys.platform == "win32":
        assert "Windows" in kind
    elif sys.platform == "darwin":
        assert "macOS" in kind
    else:
        assert "Linux" in kind
