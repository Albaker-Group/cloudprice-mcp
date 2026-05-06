# Install guide

> **TL;DR — recommended path:**
> ```bash
> pip install cloudprice-mcp
> cloudprice-mcp setup
> ```
> Then restart Claude Desktop. That's it.
>
> If you don't trust running new tools to write config files, follow the manual steps for [Windows](#windows-manual), [macOS](#macos-manual), or [Linux](#linux-manual).

---

## Auto-install (works on Windows / macOS / Linux)

```bash
# 1. Install the package from PyPI
pip install cloudprice-mcp

# 2. Auto-configure Claude Desktop (interactive — shows config + asks Y/N)
cloudprice-mcp setup
```

Then **restart Claude Desktop fully** (see [restart instructions](#restart-claude-desktop) for your OS).

### Trust spectrum — `cloudprice-mcp setup` modes

| Command | What it does |
|---|---|
| `cloudprice-mcp setup` | Detects everything, shows you the config, asks Y/N, writes it |
| `cloudprice-mcp setup --yes` | Skips the prompt — useful in scripts / CI |
| `cloudprice-mcp setup --dry-run` | Shows what it would write without modifying any files |
| `cloudprice-mcp setup --print-config` | Outputs only the JSON to stdout — for users who want to paste it manually |

### Diagnose problems

If something doesn't work after running `setup`, run:

```bash
cloudprice-mcp doctor
```

It checks Python version, package install, tool registration, config file location, cloudprice entry presence, and command path validity. Tells you exactly what's broken.

---

## Manual installation

### <a name="windows-manual"></a>🪟 Windows (manual)

#### Step 1 — Install Python 3.10+

Get from https://www.python.org/downloads/ if you don't have it. Check `--add to PATH` during install.

```powershell
python --version
# Should print 3.10.x or higher
```

#### Step 2 — Install the package

```powershell
pip install cloudprice-mcp
```

#### Step 3 — Find your Claude Desktop config path

Windows has **two possible config locations** depending on how Claude Desktop was installed:

| Install type | Config path |
|---|---|
| Microsoft Store (default for new installs in 2026) | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| Direct `.exe` from claude.ai/download | `%APPDATA%\Claude\claude_desktop_config.json` |

Run this to detect which yours is:

```powershell
@(
  "$env:LOCALAPPDATA\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json",
  "$env:APPDATA\Claude\claude_desktop_config.json"
) | ForEach-Object {
  if (Test-Path $_) { Write-Host "FOUND: $_" }
}
```

#### Step 4 — Edit the config file

Open the path that printed `FOUND` in Notepad:

```powershell
notepad "<paste the FOUND path here>"
```

If Notepad says "cannot find file" but you expected one to exist — Claude Desktop hasn't been opened yet. Open Claude Desktop, sign in, send any message (creates the config dir), then retry.

Paste this content (merge into existing JSON if there's already a `preferences` block):

```json
{
  "mcpServers": {
    "cloudprice": {
      "command": "python",
      "args": ["-m", "cloudprice_mcp.server"]
    }
  }
}
```

#### Step 5 — Kill cached subprocess (avoids stale-version gotcha)

```powershell
Get-Process | Where-Object { $_.Path -like "*cloudprice-mcp*" } | Stop-Process -Force
```

#### Step 6 — Restart Claude Desktop

For Microsoft Store install: **right-click Claude tray icon → Quit**, OR use **File → Exit** inside the Claude window. **Just X-closing the window doesn't fully quit it.**

For direct .exe install: same — right-click tray → Quit.

Wait 5 sec, reopen from Start Menu.

#### Step 7 — Verify

Click **`+`** in chat composer → **Connectors** → **cloudprice** should appear with **9 tools**.

---

### <a name="macos-manual"></a>🍎 macOS (manual)

#### Step 1 — Install Python 3.10+

```bash
python3 --version
```

If older than 3.10, install from https://www.python.org/downloads/macos/ (click the latest 3.12 or 3.13 macOS installer).

#### Step 2 — Install the package

If `pip3` errors with "externally-managed-environment" (common on macOS Sonoma+), use a venv:

```bash
python3 -m venv ~/cloudprice-venv
source ~/cloudprice-venv/bin/activate
pip install cloudprice-mcp
```

Otherwise:

```bash
pip3 install cloudprice-mcp
```

#### Step 3 — Find your Python's absolute path

**This is critical on macOS** — Claude Desktop launches with a minimal PATH that often doesn't include where `python3` lives. The config MUST use the absolute path.

```bash
# If you used pip3 directly:
which python3

# If you used the venv:
echo "$HOME/cloudprice-venv/bin/python3"
```

Save this path — you'll need it in the next step.

#### Step 4 — Edit the config file

```bash
mkdir -p "$HOME/Library/Application Support/Claude"
nano "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
```

Paste (replace `<ABSOLUTE_PYTHON_PATH>` with what you got in Step 3):

```json
{
  "mcpServers": {
    "cloudprice": {
      "command": "<ABSOLUTE_PYTHON_PATH>",
      "args": ["-m", "cloudprice_mcp.server"]
    }
  }
}
```

Save: **`Ctrl+O` → Enter → `Ctrl+X`**.

#### Step 5 — Kill cached subprocess

```bash
pkill -f cloudprice-mcp
pkill -f cloudprice_mcp
```

(Either or both is fine — silent if nothing was running.)

#### Step 6 — Restart Claude Desktop

**Cmd+Q** in Claude Desktop. **NOT just clicking the red close button** — that just hides the window on macOS, doesn't quit the app.

Wait 5 sec, reopen from Applications or Spotlight.

#### Step 7 — Verify

Click **`+`** in chat composer → **Connectors** → **cloudprice** with **9 tools**.

---

### <a name="linux-manual"></a>🐧 Linux (manual)

```bash
python3 --version           # need 3.10+
pip install cloudprice-mcp  # or use a venv if your distro requires it
which python3               # save this absolute path
```

Edit `~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cloudprice": {
      "command": "/absolute/path/to/python3",
      "args": ["-m", "cloudprice_mcp.server"]
    }
  }
}
```

Restart Claude Desktop fully. Verify in Connectors.

---

## <a name="restart-claude-desktop"></a>How to fully restart Claude Desktop

Restarting matters because Claude Desktop **caches the MCP tool list** — if you don't fully quit and respawn, you'll see the old tool list.

| Platform | How to fully quit |
|---|---|
| **Windows (Microsoft Store)** | Right-click Claude tray icon → Quit, OR File → Exit inside Claude window |
| **Windows (direct .exe)** | Right-click Claude tray icon → Quit |
| **macOS** | Cmd+Q (NOT just closing the window) |
| **Linux** | Quit from app menu or `pkill -f Claude` |

After quit, wait 5 seconds before reopening. This gives the OS time to release file locks.

---

## Troubleshooting

### "Connector doesn't appear in Claude Desktop"

Most common causes:

1. **Config file written to the wrong path** (especially on Windows — MS Store vs direct install). Run `cloudprice-mcp doctor` to find the real path.
2. **Claude Desktop wasn't fully quit** before reopening. See [restart instructions](#restart-claude-desktop).
3. **`python` / `python3` not on Claude Desktop's PATH**. Use absolute path in `command` field.

### "I see the connector but only 7 tools (not 9)"

You have an older version. Upgrade:

```bash
pip install --upgrade cloudprice-mcp
```

Then kill cached subprocess + restart Claude Desktop (see above).

### "I get `cloudprice-mcp: command not found`"

The `cloudprice-mcp.exe` shim isn't on PATH. Use the `python -m` form in the config instead:

```json
{
  "command": "python",
  "args": ["-m", "cloudprice_mcp.server"]
}
```

### "JSON syntax error popup when opening Claude Desktop"

You likely have a missing comma or bracket. Validate at https://jsonlint.com or run `cloudprice-mcp doctor`.

### "I'm on macOS, get 'externally-managed-environment' error"

Use a venv:

```bash
python3 -m venv ~/cloudprice-venv
source ~/cloudprice-venv/bin/activate
pip install cloudprice-mcp
cloudprice-mcp setup
```

The setup command will detect the venv's Python and use the right absolute path.

### Anything else

Run `cloudprice-mcp doctor` and paste the output in a [GitHub issue](https://github.com/alialbaker/cloudprice-mcp/issues).
