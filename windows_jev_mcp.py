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
    except ValueError as error:
        return {"status": "error", "message": str(error)}


def _describe(window: str) -> dict:
    return win_agent.Session(window, "", log=False).describe()


def _plan(window: str, steps: list[str], params: dict | None, save_as: str | None) -> dict:
    return win_agent.Session(window, " ; ".join(steps)).run_plan(steps, params, save_as)


def _action(window: str, action: str, params: dict | None) -> dict:
    session = win_agent.Session(window, action)
    saved = session.lib.actions.get(action)
    if saved is None:
        raise ValueError(f"no saved action {action!r} for {session.app}; saved: {sorted(session.lib.actions)}")
    missing = [p for p in saved["params"] if p not in (params or {})]
    if missing:
        raise ValueError(f"action {action!r} needs params {missing}")
    return session.run_plan(saved["steps"], params)


@mcp.tool()
def describe(window: str) -> dict:
    """Start here for an app. Returns the current window's buttons and fields, its
    screen text, the app's saved actions (name -> params), and the controls of other
    windows of this app seen before (e.g. a compose window), so a whole task can be
    planned as one run_steps call. Reading costs no clicks and grows the app library.
    """
    return _call(_describe, window)


@mcp.tool()
def run_steps(window: str, steps: list[str], params: dict | None = None, save_as: str | None = None) -> dict:
    """Run a planned list of steps in one call.

    Each step names a control as it appears on screen ("New mail", "Seven") or is
    type "<text>" into <field>, optionally ending in "and press enter". Steps may use
    {placeholders} filled from `params`. Steps can span windows: when a click opens
    a new window of the same app, the run follows it, and controls the library already
    knows are waited for (up to 3 s) while that window opens.

    Grounding is exact name first, then the app library, then one batched Jev call,
    so repeat runs need no Jev at all. Pass `save_as` to store the steps (with their
    placeholders) as a reusable action once the run succeeds; replay it with
    run_action. Stops at the first step with no matching control; clicks that changed
    nothing are flagged screen_changed=false. Returns per-step timings in ms and the
    final screen_text for verification.
    """
    return _call(_plan, window, steps, params, save_as)


@mcp.tool()
def run_action(window: str, action: str, params: dict | None = None) -> dict:
    """Replay a saved action (see describe -> saved_actions) with its params in one call."""
    return _call(_action, window, action, params)


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
