# windows-jev-mcp

An MCP server that lets an AI agent (Cursor, Claude Desktop, ...) operate Windows desktop apps quickly.
The calling agent plans the steps; [Jev](https://typesafe.ai) (TypeSafe's decision model) maps each step
to a control on screen; the server clicks and types.

- The window is read with one cached UI Automation call (`FindAllBuildCache`), typically 30-120 ms.
- All visible steps are grounded in a single Jev request; the answers are reused until the layout changes.
- Jev may answer "none", and low-confidence answers are ignored, so a step whose control is not on screen is never
  clicked on a guess.
- Text goes into fields through UI Automation `ValuePattern` (about 18 ms), with keystrokes as fallback.
- Windows that expose almost no UI Automation names fall back to OmniParser's YOLOv9 detector plus Windows OCR (optional).
- A click is refused when another window covers the target point.

Measured on Windows Calculator: `12 + 7` from exact names, including clearing a half-typed expression, in about
1.0 s with no Jev call. `1 + 1` in about 0.85 s. The same sum in plain words ("clear", "1", "2", "add", "7",
"equals sign") in about 1.6 s, one Jev call. Switching to Programmer mode the first time takes one Jev-chosen
menu click (about 1.6 s); the learned route needs no Jev call (about 1.1 s).
`1 + 1` with the animation cap is about 0.8 s. A Chrome address-bar URL is one step
(`Ctrl+L`, then Enter waits for the new title in the same call): the Konrad Zuse article
opened in about 1.1 s. The same article via Wikipedia's own search box — open the site,
click Search, type the name — is one call at about 2.8 s.

## Tools

| Tool | Use |
|---|---|
| `list_windows()` | Titles of visible top-level windows. |
| `describe(window)` | Start here. Buttons, fields and screen text of the current window, plus `reachable`: controls elsewhere in the app the graph can reach, with the clicks needed. No clicks. |
| `show_graph(app)` | Open the app's saved link tree as an HTML page (collapsible tree, filter box). `app` is a name like `olk` / `calculator` or part of a window title. |
| `edit_graph(app, control, add=None, remove=None, delete=False)` | Correct the link tree: add or remove what clicking `control` reveals, or delete its routes. |
| `explore(window, max_clicks=40)` | Grow the app graph by clicking each tab once (ribbon tabs, settings tabs). Never clicks buttons; skips names like Send, Delete, Close. |
| `run_steps(window, steps)` | Run a whole plan in one call. Each step names a control (`"Seven"`, `"New mail"`, `"Density"`) or is `type "<text>" into <field>` (optionally `... and press enter`). Enter waits for the new page inside the call, so the steps after a navigation go in the same list. A control that is not on screen but known to the graph is reached by walking the learned route first. Follows new windows the app opens. Stops at the first step that finds no control; clicks that changed nothing are flagged `screen_changed: false`. Returns per-step timings in ms, the route walked, the final `screen_text`, and the buttons and fields now visible. |
| `run_goal(window, goal, max_steps=12)` | Jev picks every step itself. When a field needs text it returns `needs_text`; answer with `provide_text`. Slower and weaker at ordered input such as numbers. |
| `provide_text(text, press_enter=False)` | Continue a paused `run_goal`. |

### Per-app graph

Each app gets a JSON file in `WINDOWS_JEV_LIBRARY` (default `~/windows_jev_library`, e.g. `olk.json` for new Outlook):

- `reveals`: control -> controls that appeared after clicking it (tab, menu, dialog, new window). Learned from every
  click in every run and from `explore`. A step whose control is not visible is reached through the shortest chain
  of these, e.g. `"Density"` in Outlook clicks `View` first.

A step is grounded in this order:

1. Exact name on screen (`"Plus"`, also `"the Plus button"`). No Jev call.
2. Exact name the graph can reach: walk the route, then click.
3. Jev, over the controls on screen plus the graph's controls on other screens. `"programmer mode"` can resolve
   to `Programmer Calculator` behind `Open Navigation`, and the route is walked.
4. Jev picks the tab or menu most likely to reveal the control, at most twice. Only navigation-like names are
   offered (never Send, Delete, Close, data rows). What the click reveals is learned, so step 2 covers it next time.

A click counts as navigation only when at least 3 controls appear. `explore` clicks only tabs, because
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

The key can also live in a `.env` file next to `win_agent.py` (`TYPESAFE_API_KEY=...`, gitignored) or in the
Windows user environment variable `TYPESAFE_API_KEY` instead of the config. Never commit it.

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
