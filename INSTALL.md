# Install guide

> **TL;DR — recommended path:**
> ```bash
> pip install cloudprice-mcp
> cloudprice-mcp setup
> ```
> Then restart whichever AI client(s) you have installed. **One command auto-configures Claude Desktop, GitHub Copilot, Cursor, Windsurf, Cline, Continue, and Zed** — whichever it detects on your machine.
>
> If you don't trust running new tools to write config files, follow the manual steps below per client.

---

## Auto-install (works on Windows / macOS / Linux)

```bash
# 1. Install the package from PyPI
pip install cloudprice-mcp

# 2. Auto-configure every installed MCP-compatible client
cloudprice-mcp setup
```

Then **restart whichever clients were configured** (see [restart instructions](#restart-clients) for your OS).

### Trust spectrum — `cloudprice-mcp setup` modes

| Command | What it does |
|---|---|
| `cloudprice-mcp setup` | Detects every installed client, shows the plan, asks Y/N once for the batch |
| `cloudprice-mcp setup --yes` | Skips the prompt — useful in scripts / CI |
| `cloudprice-mcp setup --client copilot` | Configure just one client (repeatable: `--client copilot --client cursor`) |
| `cloudprice-mcp setup --all` | Configure every known client even if not detected (creates parent dirs) |
| `cloudprice-mcp setup --force` | Refresh existing entries — useful after upgrade or moving Python |
| `cloudprice-mcp setup --dry-run` | Show per-client diffs, write nothing |
| `cloudprice-mcp setup --print-config` | Emit per-client JSON to stdout — for manual paste |
| `cloudprice-mcp setup --list-clients` | Show the detection table (which clients are known + installed) |

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

Click **`+`** in chat composer → **Connectors** → **cloudprice** should appear with **10 tools**.

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

Click **`+`** in chat composer → **Connectors** → **cloudprice** with **10 tools**.

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

## <a name="other-clients"></a>Other MCP-compatible AI clients

cloudprice-mcp is an MCP server, so any client that speaks the protocol can use it. The auto-install above already detects + configures these — these manual sections are for users who want to write the JSON themselves or troubleshoot a specific client.

`cloudprice-mcp setup --list-clients` shows the detection table any time.

### <a name="copilot-manual"></a>GitHub Copilot Agent Mode (VS Code)

Requires VS Code with GitHub Copilot Chat extension (Agent Mode shipped late 2025).

| OS | Config file path |
|---|---|
| **Windows** | `%APPDATA%\Code\User\mcp.json` |
| **Linux** | `~/.config/Code/User/mcp.json` |
| **macOS** | `~/Library/Application Support/Code/User/mcp.json` |

Or open it from VS Code: **Command Palette → "MCP: Open User Configuration"**.

```json
{
  "servers": {
    "cloudprice": {
      "type": "stdio",
      "command": "cloudprice-mcp",
      "args": []
    }
  }
}
```

> Copilot's schema uses `servers` (not `mcpServers`) and requires the `type: "stdio"` field — different from Claude Desktop. If `cloudprice-mcp` isn't on PATH, replace `command` with the absolute path to `python` (or `python3`) and set `args` to `["-m", "cloudprice_mcp.server"]`.

**Restart:** Fully quit VS Code (close all windows + verify `Code.exe` is gone from Task Manager / Activity Monitor), reopen.
**Verify:** Open Copilot Chat → switch to **Agent** mode → click the tools icon → `cloudprice` with 10 tools should appear.

### <a name="cursor-manual"></a>Cursor

| OS | Config file path |
|---|---|
| **Windows** | `%USERPROFILE%\.cursor\mcp.json` |
| **Linux / macOS** | `~/.cursor/mcp.json` |

```json
{
  "mcpServers": {
    "cloudprice": {
      "type": "stdio",
      "command": "cloudprice-mcp",
      "args": []
    }
  }
}
```

A workspace-level alternative is `.cursor/mcp.json` in your project root.

**Restart:** Fully quit Cursor and reopen.
**Verify:** Cursor → Settings → MCP → cloudprice should appear; tools become available in Composer / Chat.

### <a name="windsurf-manual"></a>Windsurf (Codeium)

| OS | Config file path |
|---|---|
| **Windows** | `%USERPROFILE%\.codeium\windsurf\mcp_config.json` |
| **Linux / macOS** | `~/.codeium/windsurf/mcp_config.json` |

```json
{
  "mcpServers": {
    "cloudprice": {
      "command": "cloudprice-mcp",
      "args": []
    }
  }
}
```

**Restart:** Fully quit Windsurf and reopen.
**Verify:** Cascade panel → MCPs icon → cloudprice should be listed.

### <a name="cline-manual"></a>Cline (VS Code extension)

Requires the [Cline](https://marketplace.visualstudio.com/items?itemName=saoudrizwan.claude-dev) VS Code extension installed and run at least once (which creates the `globalStorage` directory).

| OS | Config file path |
|---|---|
| **Windows** | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json` |
| **Linux** | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| **macOS** | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |

```json
{
  "mcpServers": {
    "cloudprice": {
      "command": "cloudprice-mcp",
      "args": []
    }
  }
}
```

**Restart:** In VS Code: open Cline panel → Settings (gear) → MCP Servers → Restart, or fully quit + reopen VS Code.
**Verify:** Cline panel → MCP Servers tab → cloudprice with status **Connected**.

### <a name="continue-manual"></a>Continue.dev

Continue uses one JSON file **per server** under a `mcpServers` folder.

| OS | Config file path |
|---|---|
| **Windows** | `%USERPROFILE%\.continue\mcpServers\cloudprice.json` |
| **Linux / macOS** | `~/.continue/mcpServers/cloudprice.json` |

```json
{
  "name": "cloudprice",
  "command": "cloudprice-mcp",
  "args": []
}
```

> Continue's MCP support requires **Agent mode** in the chat panel. The schema is per-file (each file is one server entry), unlike the dict-keyed `mcpServers` of other clients.

**Restart:** Reload Continue (VS Code: `Cmd+Shift+P` → `Continue: Reload`) or fully quit + reopen the editor.
**Verify:** Continue panel → MCP Servers → cloudprice should be listed.

### <a name="zed-manual"></a>Zed

Add to your existing Zed `settings.json` (don't replace it — merge `context_servers` into the top-level object).

| OS | Config file path |
|---|---|
| **Windows** | `%APPDATA%\Zed\settings.json` |
| **Linux / macOS** | `~/.config/zed/settings.json` |

```json
{
  "context_servers": {
    "cloudprice": {
      "command": "cloudprice-mcp",
      "args": []
    }
  }
}
```

> Zed uses `context_servers` (top-level, no longer under `experimental`).

**Restart:** Fully quit Zed and reopen.
**Verify:** Agent Panel → Settings → cloudprice should appear under Context Servers.

---

## <a name="restart-clients"></a>How to fully restart your AI client

Restarting matters because most clients **cache the MCP tool list at launch** — if you don't fully quit and respawn, you'll see the stale list.

| Client | How to fully quit |
|---|---|
| **Claude Desktop (Windows MS Store)** | Right-click tray icon → Quit, OR File → Exit inside Claude window |
| **Claude Desktop (Windows direct .exe)** | Right-click tray icon → Quit |
| **Claude Desktop (macOS)** | Cmd+Q (NOT just closing the window) |
| **Claude Desktop (Linux)** | Quit from app menu or `pkill -f Claude` |
| **VS Code (Copilot / Cline)** | Close all windows; verify no `Code.exe` / `code` process remains (Task Manager / `ps`) |
| **Cursor** | Cmd+Q (macOS) / File → Exit (Windows / Linux) |
| **Windsurf** | Cmd+Q / File → Exit |
| **Continue** | `Cmd+Shift+P` → `Continue: Reload` (or quit the host editor) |
| **Zed** | Cmd+Q / File → Quit Zed |

After quit, wait 5 seconds before reopening. This gives the OS time to release file locks.

---

## Troubleshooting

### "Connector / cloudprice doesn't appear in my AI client"

Most common causes (in order of likelihood):

1. **The client wasn't fully quit** before reopening. Most clients cache the MCP tool list at launch. See [restart instructions](#restart-clients).
2. **Config file written to the wrong path** (especially on Windows — MS Store vs direct install for Claude Desktop). Run `cloudprice-mcp doctor` to find the real path per client.
3. **`python` / `python3` not on the client's PATH**. macOS clients in particular launch with a minimal PATH. Run `cloudprice-mcp setup --force` — that uses `sys.executable` (the absolute Python path) automatically.

### "I see the connector but the tool count looks wrong"

You have an older version. Upgrade and refresh:

```bash
pip install --upgrade cloudprice-mcp
cloudprice-mcp setup --force
```

Then fully quit + reopen the client. v0.5+ ships **10 tools**.

### "I get `cloudprice-mcp: command not found` (or PowerShell can't recognize it)"

The `cloudprice-mcp.exe` shim isn't on your shell's PATH. This is common when:
- You ran `pip install` but the Python `Scripts/` folder isn't on PATH (Windows installer's "Add Python to PATH" was unchecked, or only `python.exe` got PATH'd — not `Scripts\`).
- You're running from a virtual environment that hasn't been activated in this shell.
- You did an editable install (`pip install -e .`) into a venv whose bin/Scripts directory isn't on the global PATH.

**Three fixes — pick whichever fits:**

**1. Use the absolute path (works anywhere, no shell setup):**

Find the shim:
```bash
# macOS / Linux
python3 -m pip show -f cloudprice-mcp | grep cloudprice-mcp$

# Windows PowerShell
python -m pip show -f cloudprice-mcp | Select-String "cloudprice-mcp\.exe"

# Windows cmd
python -m pip show -f cloudprice-mcp | findstr "cloudprice-mcp.exe"
```

Then call it directly:
```powershell
# Windows
C:\Path\To\Python\Scripts\cloudprice-mcp.exe setup
# Or, if installed to a venv at D:\myproj\.venv:
D:\myproj\.venv\Scripts\cloudprice-mcp.exe setup
```
```bash
# macOS / Linux
~/.local/bin/cloudprice-mcp setup
# Or venv equivalent:
~/myproj/.venv/bin/cloudprice-mcp setup
```

**2. Activate the venv first:**

```powershell
# Windows PowerShell
D:\myproj\.venv\Scripts\Activate.ps1
cloudprice-mcp setup
```
```bash
# macOS / Linux
source ~/myproj/.venv/bin/activate
cloudprice-mcp setup
```

The `cloudprice-mcp` command works for the rest of that shell session.

**3. Use the `python -m` form (always works if `python` is on PATH):**

```bash
python -m cloudprice_mcp.cli setup --list-clients
python -m cloudprice_mcp.cli setup --force
```

This is also what `cloudprice-mcp setup` writes into the client config files by default — so the **client launches it** via the absolute Python path even when the shim isn't on PATH.

### "I want PATH-wide access on Windows after `pip install`"

Add Python's `Scripts\` folder to your PATH:

```powershell
# Find your Scripts folder
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
# Example output: C:\Python311\Scripts

# Add to user PATH (one-time)
$scripts = python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
[Environment]::SetEnvironmentVariable("Path", "$([Environment]::GetEnvironmentVariable('Path','User'));$scripts", "User")
```

Open a fresh PowerShell window — `cloudprice-mcp` should now resolve everywhere.

### "JSON syntax error popup when opening the client"

You likely have a missing comma or bracket from a manual edit. Validate at https://jsonlint.com or run `cloudprice-mcp doctor` (it parses each client's config and flags broken JSON with the exact line/column).

### "I'm on macOS, get 'externally-managed-environment' error"

Use a venv:

```bash
python3 -m venv ~/cloudprice-venv
source ~/cloudprice-venv/bin/activate
pip install cloudprice-mcp
cloudprice-mcp setup
```

The setup command uses `sys.executable` (the venv's python), so the config it writes works even after you close the activated shell.

### Running from a development checkout

If you cloned the repo and want to test changes (`pip install -e .`):

```bash
git clone https://github.com/alialbaker/cloudprice-mcp.git
cd cloudprice-mcp
python -m venv .venv

# Activate the venv
.venv\Scripts\Activate.ps1     # Windows PowerShell
source .venv/bin/activate      # macOS / Linux

pip install -e ".[dev]"
cloudprice-mcp setup --list-clients
```

If you switch shells, either reactivate the venv or call the shim by full path: `D:\path\to\.venv\Scripts\cloudprice-mcp.exe setup`.

The setup command will detect the venv's Python and use the right absolute path.

### Anything else

Run `cloudprice-mcp doctor` and paste the output in a [GitHub issue](https://github.com/alialbaker/cloudprice-mcp/issues).
