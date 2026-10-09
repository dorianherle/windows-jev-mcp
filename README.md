# windows-jev-mcp

An MCP server that lets an AI agent (Cursor, Claude Desktop, ...) operate Windows desktop apps quickly.
The calling agent plans the steps; [Jev](https://typesafe.ai) (TypeSafe's decision model) maps each step
to a control on screen; the server clicks and types.

- The window is read with one cached UI Automation call (`FindAllBuildCache`), typically 30-120 ms.
- All visible steps are grounded in a single Jev request; the answers are reused until the layout changes.
- Text goes into fields through UI Automation `ValuePattern` (about 18 ms), with keystrokes as fallback.
- Windows that expose almost no UI Automation names fall back to OmniParser's YOLOv9 detector plus Windows OCR (optional).

Measured on Windows Calculator: a 6-click calculation in about 1.1 s end to end, one Jev call, about 110 ms per step after the first.

## Tools

| Tool | Use |
|---|---|
| `list_windows()` | Titles of visible top-level windows. |
| `run_steps(window, steps)` | Fastest and most reliable. Each step names a control as it appears on screen (`"Seven"`, `"Add New Tab"`) or is `type "<text>" into <field>` (optionally `... and press enter`). Stops at the first step that finds no control or changes nothing. Returns per-step timings in ms and the window's `screen_text` for verification. |
| `run_goal(window, goal, max_steps=12)` | Jev picks every step itself. When a field needs text it returns `needs_text`; answer with `provide_text`. Slower and weaker at ordered input such as numbers. |
| `provide_text(text, press_enter=False)` | Continue a paused `run_goal`. |

Every run writes screenshots, annotated candidates and a README with timings to `WINDOWS_JEV_LOGS`
(default `~/windows_jev_runs`).

## Setup

Requirements: Windows 10/11, Python 3.11+, a TypeSafe API key.

```powershell
git clone <this repo> windows-jev-mcp
cd windows-jev-mcp
python -m pip install -r requirements.txt
python win_agent.py --check   # prints "self-check ok"
```

Add the server to your MCP client. For Cursor, `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "windows-jev": {
      "command": "C:\\path\\to\\python.exe",
      "args": ["C:\\path\\to\\windows-jev-mcp\\windows_jev_mcp.py"],
      "env": {
        "TYPESAFE_API_KEY": "<your key>",
        "WINDOWS_JEV_LOGS": "C:\\path\\to\\logs"
      }
    }
  }
}
```

The key can also live in the Windows user environment variable `TYPESAFE_API_KEY` instead of the config.
Never commit it.

### Optional: vision fallback

Only needed for apps whose controls have no UI Automation names (some custom-drawn or legacy UIs).

```powershell
python -m pip install -r requirements-vision.txt
git clone https://github.com/microsoft/OmniParser $HOME\OmniParser   # or set OMNIPARSER_DIR
```

On first use the YOLOv9 weights are downloaded from Hugging Face (`microsoft/OmniParser-v2.0`) and converted
to OpenVINO; inference runs on an Intel GPU if present, otherwise the CPU. Note the OmniParser detector weights
carry their own license (AGPL); check it before redistributing.

## Command line

```powershell
python win_agent.py --window "Calculator" --goal "compute 12 + 7"
```

## Known limits

- A click with no pixel change within 600 ms counts as dead; very slow apps need a larger `settle` timeout.
- Jev's choice takes at most 255 options; windows with more controls keep the first 250.
- Jev sees text only; icon-only controls without names are labelled by position and nearby text.
