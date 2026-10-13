# windows-jev-mcp

Yes, you can use it. It is an MCP server that clicks and types in real Windows apps.

You name the steps. The server finds each control on screen and does it. A step that matches a visible name does not call a model. When the name is only a description ("Evaporation" for a long button), it still matches if that button is the only one. [Jev](https://typesafe.ai) is used only when the name is not on screen.

A full unknown-UI pass is in this repo: Chrome opened [chipcake.com](https://chipcake.com), added evaporation of Al, spin coat, and develop. Score **5/5** in **14.7 s**, with no Jev call. Calculator `1 + 1` is under a second.

It will not click Send, Delete, or similar names while it is searching for a control. Do not point it at those unless you mean it.

## Install

Windows 10/11, Python 3.11+.

```powershell
git clone https://github.com/dorianherle/windows-jev-mcp.git
cd windows-jev-mcp
python -m pip install -r requirements.txt
python win_agent.py --check
```

`self-check ok` means the install is fine. No window is opened.

Put your TypeSafe key in a file named `.env` next to `win_agent.py`:

```
TYPESAFE_API_KEY=your-key-here
```

That file is gitignored. Never commit it. A key from [typesafe.ai](https://typesafe.ai) is only needed when a control name is not already on screen.

Cursor: `~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "windows-jev": {
      "command": "C:\\path\\to\\python.exe",
      "args": ["C:\\path\\to\\windows-jev-mcp\\windows_jev_mcp.py"]
    }
  }
}
```

Restart the MCP server in the client after you change this. The key can also go in that `env` block, or in the Windows user variable `TYPESAFE_API_KEY`.

## Prompt for an agent

Paste this to the agent that should wire it up and test an app:

```
Set up https://github.com/dorianherle/windows-jev-mcp and use it to test a Windows app by clicking its real UI.

Install
- Windows 10/11, Python 3.11+.
- Clone the repo, pip install -r requirements.txt, run python win_agent.py --check. Stop if it does not print "self-check ok".
- Ask me for a TypeSafe API key if .env is missing. Write it only to a gitignored .env next to win_agent.py as TYPESAFE_API_KEY=... Never commit that file, never print the key.
- Register windows_jev_mcp.py as an MCP server named windows-jev. Restart that server before the first test.

Test the app through its UI
1. Launch the app, or use a window that is already open. Take a unique part of the window title from list_windows.
2. Call describe(window). It does not click. Read buttons, fields, and screen_text. Plan from those names.
3. Call run_steps once with a short list. Each step is a control name ("+ add step", "Seven") or: type "the text" into Field name and press enter. Use the shortest unique name. After Enter, the next steps in the same list already see the new page.
4. Do not click Send, Delete, pay, submit, or close unless I explicitly asked for that step.
5. Score the return value and show me all four:
   - total: how many checks you defined
   - time: total_ms from the result
   - log: status, the steps list, and the screenshot folder (log)
   - score: how many checks are true in buttons, fields, screen_text, and step actions
6. If status starts with "stopped:", that one control was not on screen. describe again and replace that step. Do not wander off into a different page.
7. A click with screen_changed false did not land. Say so.

Example pass: window "Calculator", steps ["1", "Plus", "1", "Equals"]. The check passes when the result text contains "2".
```

## Test an app yourself

Open the app so its window is visible.

```text
list_windows()
describe("Calculator")
run_steps("Calculator", ["1", "Plus", "1", "Equals"])
```

`describe` only reads. `run_steps` clicks the whole list in one call. A URL can be one step: `type "https://example.com" into Address and press enter`.

Judge the result:

| Field | Meaning |
|---|---|
| total | Number of checks you care about |
| time | `total_ms` on the result |
| log | `status`, each step's `action`, and the `log` folder of screenshots |
| score | How many checks show up in `buttons`, `fields`, `screen_text`, or the step actions |

Screenshots land in `~/windows_jev_runs` (or `WINDOWS_JEV_LOGS`). `stepN_screen.png` is the window before that click.

The ChipCake test in this repo is the same idea, scored automatically:

```powershell
python chipcake_test.py
```

It opens Chrome, builds the stack, and writes `chipcake_result.json` with `total`, `time`, `log`, and `score`. It clicks the live desktop, so run it on a machine you can spare. `python chipcake_test.py --check` only checks the scorer.

## Tools

| Tool | What it does |
|---|---|
| `list_windows()` | Visible window titles. |
| `describe(window)` | Buttons, fields, screen text. No clicks. |
| `run_steps(window, steps)` | Click and type a plan. This is the one to use. |
| `run_goal(window, goal)` | Let Jev pick every click. Slower. Use `run_steps` when you can name the controls. |
| `provide_text(text)` | Continue a `run_goal` that stopped to ask what to type. |
| `explore(window)` | Click tabs only, to learn the app. Skips Send, Delete, Close. |
| `show_graph(app)` | Open the learned click-paths as a page. |
| `edit_graph(...)` | Fix a learned path. |

A step is resolved in order: exact name, unique longer name, a path learned from an earlier click, then one Jev call. Jev may answer "none". Low-confidence picks are dropped, so a missing control is not clicked as a guess.

Learned paths are saved per app in `~/windows_jev_library`.

## Limits

- Another window covering the button refuses the click.
- A click that changes no pixels within 600 ms is marked dead (`screen_changed: false`).
- Jev sees at most 250 controls, and it sees text, not bare icons.
- Apps with no UI Automation names need the optional vision extra (`requirements-vision.txt` and [OmniParser](https://github.com/microsoft/OmniParser)). The detector weights are AGPL. Check that license before redistributing them.
