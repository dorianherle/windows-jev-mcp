"""MCP server: drive a Windows app toward a goal; ask the caller when text must be typed.

Setup and client config: see README.md.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import win_agent


def _init_com() -> None:
    sys.coinit_flags = 0
    import comtypes

    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)


# UI Automation objects and the open session must stay on the thread that created them.
_worker = ThreadPoolExecutor(max_workers=1, initializer=_init_com)
_session: win_agent.Session | None = None
mcp = FastMCP("windows-jev")


def _start(window: str, goal: str, max_steps: int) -> dict:
    global _session
    _session = win_agent.Session(window, goal, max_steps)
    return _session.advance()


@mcp.tool()
def list_windows() -> list[str]:
    """Titles of the visible top-level windows. Use one as `window` in run_goal."""
    return [title for _hwnd, title in win_agent.enum_windows()]


def _call(fn, *args) -> dict:
    try:
        return _worker.submit(fn, *args).result()
    except Exception as error:
        return {"status": "error", "message": f"{type(error).__name__}: {error}"}


def _describe(window: str) -> dict:
    return win_agent.Session(window, "", log=False).describe()


def _plan(window: str, steps: list[str]) -> dict:
    return win_agent.Session(window, " ; ".join(steps)).run_plan(steps)


def _explore(window: str, max_clicks: int) -> dict:
    return win_agent.Session(window, "explore", log=False).explore(max_clicks)


@mcp.tool()
def describe(window: str) -> dict:
    """Start here for an app. Returns the current window's buttons and fields, its
    screen text, and `reachable`: every control on other screens of this app the
    graph knows how to reach, with the number of clicks needed. Any of those names
    can be used directly as a run_steps step; the route is walked automatically.
    Reading costs no clicks.
    """
    return _call(_describe, window)


@mcp.tool()
def explore(window: str, max_clicks: int = 40) -> dict:
    """Grow the app's graph by clicking each tab (ribbon tabs, settings tabs) once.
    Only tab controls are clicked, never buttons, and names like Send, Delete or
    Close are skipped. Returns the screens and links known and what is now reachable.
    """
    return _call(_explore, window, max_clicks)


def _show(app: str) -> dict:
    lib = win_agent.library_for(app)
    page = lib.path.with_suffix(".html")
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(lib.html(), encoding="utf-8")
    os.startfile(page)
    return {"app": lib.app, "page": str(page), "routes": len(lib.reveals)}


def _edit(app: str, control: str, add: list[str] | None, remove: list[str] | None, delete: bool) -> dict:
    lib = win_agent.library_for(app)
    lib.edit(control, add, remove, delete)
    lib.save()
    return {"app": lib.app, "control": control, "reveals": lib.reveals.get(control, []), "routes": len(lib.reveals)}


@mcp.tool()
def show_graph(app: str) -> dict:
    """Open the app's saved link tree as an HTML page in the browser.

    `app` is an app name ("olk", "calculator") or part of an open window's title.
    Each control expands into the controls clicking it reveals; the page has a filter box.
    """
    return _call(_show, app)


@mcp.tool()
def edit_graph(
    app: str, control: str, add: list[str] | None = None, remove: list[str] | None = None, delete: bool = False
) -> dict:
    """Edit the app's link tree: `add` / `remove` names from what clicking `control`
    reveals, or `delete` the control's routes entirely. Takes effect for the next run.
    """
    return _call(_edit, app, control, add, remove, delete)


@mcp.tool()
def run_steps(window: str, steps: list[str]) -> dict:
    """Run a planned list of steps in one call.

    Each step names a control ("New mail", "Seven", "Table") or is
    type "<text>" into <field>, optionally ending in "and press enter". A control
    that is not on screen but known in the app graph (see describe -> reachable) is
    reached by walking the shortest known click path first, so only the target
    needs to be named. Runs follow new windows the app opens and wait up to 3 s for
    known controls while a window opens. Every click grows the graph.

    Grounding is exact name, then the graph, then one batched Jev call that also sees
    controls on other screens of the graph. A control still not found is searched for:
    Jev picks a tab or menu to open (at most 2 clicks, never Send/Delete-like names).
    Jev answers below confidence are ignored, so nothing is clicked on a guess. Enter waits
    for the new page inside this call: put the steps that happen after a navigation in this
    same list, instead of returning to plan them. Stops at the first step with no matching
    control; clicks that changed nothing are flagged screen_changed=false. Returns per-step
    timings in ms (with the route walked), the final screen_text, and the buttons and fields
    now on screen. The window title is read again after the run.
    """
    return _call(_plan, window, steps)


@mcp.tool()
def run_goal(window: str, goal: str, max_steps: int = 12) -> dict:
    """Drive the window whose title contains `window` toward `goal` by clicking.

    Prefer run_steps when you can list the clicks; run_goal lets Jev pick every
    step itself, which is slower and weaker at ordered input such as numbers.
    Returns status "done", a "stopped: ..." reason, or "needs_text". On
    needs_text, decide the exact text for the named field and call
    provide_text; the same run continues from there. Each result has
    per-step timings in ms and a log folder with screenshots.
    """
    return _call(_start, window, goal, max_steps)


@mcp.tool()
def provide_text(text: str, press_enter: bool = False) -> dict:
    """Answer a needs_text result: type `text` into the requested field, then continue the run."""
    if _session is None:
        return {"status": "error", "message": "no run in progress; call run_goal first"}
    return _worker.submit(_session.provide_text, text, press_enter).result()


if __name__ == "__main__":
    mcp.run()
