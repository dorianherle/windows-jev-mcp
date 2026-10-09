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
| `describe(window)` | Start here. Buttons, fields and screen text of the current window, plus `reachable`: controls elsewhere in the app the graph can reach, with the clicks needed. No clicks. |
| `explore(window, max_clicks=40)` | Grow the app graph by clicking each tab once (ribbon tabs, settings tabs). Never clicks buttons; skips names like Send, Delete, Close. |
| `run_steps(window, steps)` | Run a whole plan in one call. Each step names a control (`"Seven"`, `"New mail"`, `"Density"`) or is `type "<text>" into <field>` (optionally `... and press enter`). A control that is not on screen but known to the graph is reached by walking the learned route first. Follows new windows the app opens. Stops at the first step that finds no control; clicks that changed nothing are flagged `screen_changed: false`. Returns per-step timings in ms, the route walked, and the final `screen_text`. |
| `run_goal(window, goal, max_steps=12)` | Jev picks every step itself. When a field needs text it returns `needs_text`; answer with `provide_text`. Slower and weaker at ordered input such as numbers. |
| `provide_text(text, press_enter=False)` | Continue a paused `run_goal`. |

### Per-app graph

Each app gets a JSON file in `WINDOWS_JEV_LIBRARY` (default `~/windows_jev_library`, e.g. `olk.json` for new Outlook):

- `reveals`: control -> controls that appeared after clicking it (tab, menu, dialog, new window). Learned from every
  click in every run and from `explore`. A step whose control is not visible is reached through the shortest chain
  of these, e.g. `"Density"` in Outlook clicks `View` first.
- `grounding`: step text -> control name, learned from steps that worked.

A step is grounded by exact name, then `grounding`, then a graph route, and only then by Jev, so a known app needs
no Jev call. A click counts as navigation only when at least 3 controls appear. `explore` clicks only tabs, because
clicking unknown buttons in apps like Outlook can send, delete or move data. Delete or edit the JSON to correct it.

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
