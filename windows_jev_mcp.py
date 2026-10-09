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


def _plan(window: str, steps: list[str]) -> dict:
    return win_agent.Session(window, " ; ".join(steps)).run_plan(steps)


@mcp.tool()
def run_steps(window: str, steps: list[str]) -> dict:
    """Fastest and most reliable: you plan, Jev only finds each control.

    Each step is one control to click, named as it appears on screen ("Seven",
    "Add New Tab", "File"), or a typing step: type "<text>" into <field>, with an
    optional trailing "and press enter". Steps whose controls are already visible
    are grounded in a single Jev call. Stops at the first step that finds no
    control; a step whose click changed nothing is flagged screen_changed=false.
    Returns per-step timings in ms and screen_text (static text and field
    contents) so you can verify the result.
    """
    try:
        return _worker.submit(_plan, window, steps).result()
    except ValueError as error:
        return {"status": "error", "message": str(error)}


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
    try:
        return _worker.submit(_start, window, goal, max_steps).result()
    except ValueError as error:
        return {"status": "error", "message": str(error)}


@mcp.tool()
def provide_text(text: str, press_enter: bool = False) -> dict:
    """Answer a needs_text result: type `text` into the requested field, then continue the run."""
    if _session is None:
        return {"status": "error", "message": "no run in progress; call run_goal first"}
    return _worker.submit(_session.provide_text, text, press_enter).result()


if __name__ == "__main__":
    mcp.run()
