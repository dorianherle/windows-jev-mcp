"""Windows UI agent: read the window -> Jev picks a control -> click or ask for text.

The window is read with one cached UI Automation call. Windows that expose
almost no names fall back to YOLOv9 + OCR on a screenshot. Jev only sees
text labels. When the next step is typing, the session pauses and returns
`needs_text`; the caller supplies the text with `provide_text`.

    python win_agent.py --window "Calculator" --goal "compute 12 + 7"
    python win_agent.py --check
"""

from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import math
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    pass

from PIL import Image, ImageChops, ImageDraw, ImageFont

RUNS = Path(os.environ.get("WINDOWS_JEV_LOGS") or Path.home() / "windows_jev_runs")
LIBRARY = Path(os.environ.get("WINDOWS_JEV_LIBRARY") or Path.home() / "windows_jev_library")
JEV_HOST = "api.typesafe.ai"
JEV_MODEL = "jev-1.13.0"
# ponytail: Jev Choice takes at most 255 options, so later candidates are dropped.
# Upgrade path is a first Choice over screen regions, then a second over that region.
MAX_CANDIDATES = 250
DONE_THRESHOLD = 0.7
# A window with fewer named controls than this is treated as opaque to UI Automation.
UIA_MIN_CONTROLS = 8
user32 = ctypes.windll.user32
_log_pool = ThreadPoolExecutor(max_workers=1)


# ---------------------------------------------------------------- windows


def enum_windows() -> list[tuple[int, str]]:
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if user32.IsWindowVisible(hwnd) and length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            found.append((int(hwnd), buf.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Visible frame, without the invisible resize border GetWindowRect includes."""
    rect = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)):
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


_grabbers = threading.local()


def grab(rect: tuple[int, int, int, int]) -> Image.Image:
    import mss

    sct = getattr(_grabbers, "sct", None)
    if sct is None:
        sct = _grabbers.sct = getattr(mss, "MSS", mss.mss)()
    left, top, right, bottom = rect
    raw = sct.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


# ---------------------------------------------------------------- input


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    item = _INPUT(type=1)
    item.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return item


def type_text(text: str) -> None:
    """Send text as Unicode keystrokes, one character at a time. '\\n' presses Enter."""
    # ponytail: modern apps (WinUI Notepad) resolve a queued VK_PACKET to the most recent
    # character, so a burst turns into "Hello !!!!". Pacing costs ~5 ms per character;
    # for long text switch to a clipboard paste that restores the previous clipboard.
    for ch in text:
        if ch == "\n":
            events = [_key(vk=0x0D), _key(vk=0x0D, flags=0x0002)]
        else:
            units = ch.encode("utf-16-le")
            events = []
            for i in range(0, len(units), 2):
                code = int.from_bytes(units[i : i + 2], "little")
                events += [_key(scan=code, flags=0x0004), _key(scan=code, flags=0x0004 | 0x0002)]
        user32.SendInput(len(events), (_INPUT * len(events))(*events), ctypes.sizeof(_INPUT))
        time.sleep(0.005)


def focus(hwnd: int) -> None:
    # Sharing the foreground thread's input state lets SetForegroundWindow succeed
    # without a synthetic Alt tap, which would put the app into menu-shortcut mode.
    foreground = user32.GetForegroundWindow()
    if foreground == hwnd:
        return
    ours = ctypes.windll.kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(foreground, None)
    user32.AttachThreadInput(ours, theirs, True)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(ours, theirs, False)


def click(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def settle(rect: tuple[int, int, int, int], before: Image.Image, timeout: float = 0.6) -> tuple[float, bool]:
    """Wait until the window changed and then stopped changing."""
    # ponytail: 600 ms with no pixel change counts as a dead click; an app that reacts
    # slower than that gets its control marked dead. Raise the timeout for such apps.
    def small(image: Image.Image) -> Image.Image:
        # A 4x4 box average is 4x cheaper than resize() and a 1 px caret still moves it by ~64.
        return image.convert("L").reduce(4)

    def differs(a: Image.Image, b: Image.Image) -> bool:
        # Any pixel changing counts, so a single updated digit is seen.
        return ImageChops.difference(a, b).point(lambda v: 255 if v > 24 else 0).getbbox() is not None

    first = small(before)
    previous = first
    changed = False
    t0 = time.perf_counter()
    still_since = t0
    while time.perf_counter() - t0 < timeout:
        # No sleep: a desktop grab already waits for the next composed frame (~17 ms).
        now = small(grab(rect))
        t = time.perf_counter()
        changed = changed or differs(first, now)
        if differs(previous, now):
            still_since = t
        previous = now
        # 40 ms spans more than two 60 Hz frames, so a running animation cannot look still.
        if changed and t - still_since >= 0.04:
            break
    return (time.perf_counter() - t0) * 1000, changed


# ---------------------------------------------------------------- reading the window


def _region(cx: float, cy: float, width: int, height: int) -> str:
    col = "left" if cx < width / 3 else "right" if cx > 2 * width / 3 else "center"
    row = "top" if cy < height / 3 else "bottom" if cy > 2 * height / 3 else "middle"
    return f"{row}-{col}"


def _number(out: list[dict]) -> list[dict]:
    out = out[:MAX_CANDIDATES]
    for index, item in enumerate(out):
        item["id"] = f"c{index}"
    return out


# Pane, Group, Window, Custom, Separator, ScrollBar, Thumb: layout, not targets.
_UIA_SKIP_TYPES = {50033, 50026, 50032, 50025, 50038, 50014, 50027}
# Edit, Document, ComboBox accept typed text.
_UIA_EDITABLE_TYPES = {50004, 50030, 50003}
_uia = None


def uia_candidates(hwnd: int, rect: tuple[int, int, int, int]) -> list[dict]:
    """All named on-screen controls in one cross-process call (FindAllBuildCache)."""
    global _uia
    import comtypes

    if _uia is None:
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U

        client = comtypes.CoCreateInstance(
            U.CUIAutomation._reg_clsid_, interface=U.IUIAutomation, clsctx=comtypes.CLSCTX_INPROC_SERVER
        )
        request = client.CreateCacheRequest()
        for prop in (
            U.UIA_NamePropertyId,
            U.UIA_ControlTypePropertyId,
            U.UIA_BoundingRectanglePropertyId,
            U.UIA_IsEnabledPropertyId,
            U.UIA_IsOffscreenPropertyId,
            U.UIA_ValueValuePropertyId,
            U.UIA_ValueIsReadOnlyPropertyId,
            U.UIA_IsValuePatternAvailablePropertyId,
            U.UIA_IsTextPatternAvailablePropertyId,
            U.UIA_IsKeyboardFocusablePropertyId,
        ):
            request.AddProperty(prop)
        request.TreeScope = U.TreeScope_Element
        request.AutomationElementMode = U.AutomationElementMode_None
        # Filtering in the query halves the elements marshalled from busy web apps (Outlook).
        condition = client.CreateAndConditionFromArray([
            client.CreatePropertyCondition(U.UIA_IsControlElementPropertyId, True),
            client.CreatePropertyCondition(U.UIA_IsOffscreenPropertyId, False),
            client.CreatePropertyCondition(U.UIA_IsEnabledPropertyId, True),
            client.CreateNotCondition(client.CreatePropertyCondition(U.UIA_NamePropertyId, "")),
        ])
        _uia = (U, client, request, condition)
    U, client, request, condition = _uia
    found = client.ElementFromHandle(hwnd).FindAllBuildCache(U.TreeScope_Descendants, condition, request)
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    out = []
    seen = set()
    for index in range(found.Length):
        element = found.GetElement(index)
        try:
            name = (element.CachedName or "").strip()[:60]
            kind = element.CachedControlType
            box = element.CachedBoundingRectangle
            hidden = element.CachedIsOffscreen or not element.CachedIsEnabled
            read_only = element.GetCachedPropertyValue(U.UIA_IsValuePatternAvailablePropertyId) is True and (
                element.GetCachedPropertyValue(U.UIA_ValueIsReadOnlyPropertyId) is True
            )
            rich = element.GetCachedPropertyValue(U.UIA_IsTextPatternAvailablePropertyId) is True and (
                element.GetCachedPropertyValue(U.UIA_IsKeyboardFocusablePropertyId) is True
            )
        except comtypes.COMError:  # UIA_E_ELEMENTNOTAVAILABLE: removed while the app redraws (WebView2)
            continue
        # A web page's document reports its URL as a read-only value; a contenteditable box
        # (Outlook's To line) is a focusable Group with a TextPattern and no ValuePattern.
        editable = not read_only and (kind in _UIA_EDITABLE_TYPES or (rich and kind != 50030))
        if not name or hidden or (kind in _UIA_SKIP_TYPES and not editable):
            continue
        x1, y1 = max(0, box.left - left), max(0, box.top - top)
        x2, y2 = min(width, box.right - left), min(height, box.bottom - top)
        if x2 <= x1 or y2 <= y1 or (name, x1, y1) in seen:
            continue
        seen.add((name, x1, y1))
        label = f"{name} ({_region((x1 + x2) / 2, (y1 + y2) / 2, width, height)})"
        if editable:
            try:
                value = element.GetCachedPropertyValue(U.UIA_ValueValuePropertyId)
            except comtypes.COMError:
                value = None
            label += f" [editable, contains '{value[:40]}']" if isinstance(value, str) and value else " [editable, empty]"
        out.append(
            {
                "text": name, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                "kind": "uia_text" if kind == 50020 else "uia", "ctype": kind, "editable": editable, "label": label,
            }
        )
    return out


def set_value(x: int, y: int, text: str) -> bool:
    """Replace the text of the editable control at screen (x, y) via UIA ValuePattern."""
    if _uia is None:
        return False
    U, client, _, _ = _uia
    try:
        element = client.ElementFromPoint(U.tagPOINT(x, y))
        pattern = element.GetCurrentPattern(U.UIA_ValuePatternId)
        if not pattern:
            return False
        pattern = pattern.QueryInterface(U.IUIAutomationValuePattern)
        if pattern.CurrentIsReadOnly:
            return False
        pattern.SetValue(text)
        return True
    except Exception:
        return False


def pick_lang() -> str:
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine

    for tag in ("en", "en-US", "de", "de-DE", "fr", "fr-FR"):
        if OcrEngine.is_language_supported(Language(tag)):
            return tag
    raise RuntimeError("no Windows OCR language installed")


def split_line(words: list[tuple[str, dict]]) -> list[dict]:
    """Split one OCR line where the gap between words is wider than a phrase gap.

    OCR joins a row of buttons ("7 8 9 x") or a menu bar into one line; each
    button must be its own click target.
    """
    words = sorted((w for w in words if w[0] and w[1].get("width")), key=lambda w: w[1]["x"])
    segments: list[list[tuple[str, dict]]] = []
    for word in words:
        if segments:
            last = segments[-1][-1][1]
            gap = word[1]["x"] - (last["x"] + last["width"])
            if gap <= 1.2 * max(last["height"], word[1]["height"]):
                segments[-1].append(word)
                continue
        segments.append([word])
    out = []
    for segment in segments:
        rects = [r for _text, r in segment]
        out.append(
            {
                "text": " ".join(text for text, _r in segment)[:60],
                "x1": int(min(r["x"] for r in rects)),
                "y1": int(min(r["y"] for r in rects)),
                "x2": int(max(r["x"] + r["width"] for r in rects)),
                "y2": int(max(r["y"] + r["height"] for r in rects)),
            }
        )
    return out


def ocr_lines(img: Image.Image, lang: str) -> list[dict]:
    import winocr

    raw = winocr.recognize_pil_sync(img, lang)
    lines = []
    for line in raw.get("lines") or []:
        words = [
            (str(w.get("text") or "").strip(), w.get("bounding_rect") or {})
            for w in line.get("words") or []
        ]
        lines.extend(split_line([(t, r) for t, r in words if isinstance(r, dict)]))
    return lines


def _center(item: dict) -> tuple[float, float]:
    return (item["x1"] + item["x2"]) / 2, (item["y1"] + item["y2"]) / 2


def build_candidates(lines: list[dict], boxes: list[list[float]], size: tuple[int, int]) -> list[dict]:
    """Vision fallback: OCR text lines plus YOLOv9 boxes that contain no text."""
    width, height = size
    out = []
    centers = [_center(line) for line in lines]
    for line, (cx, cy) in zip(lines, centers):
        out.append({**line, "kind": "text", "editable": False, "label": f"{line['text']} ({_region(cx, cy, width, height)})"})
    for x1, y1, x2, y2 in boxes:
        if (x2 - x1) * (y2 - y1) > 0.06 * width * height:
            continue
        if any(x1 <= lx <= x2 and y1 <= ly <= y2 for lx, ly in centers):
            continue
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        hint = ""
        if lines:
            near = min(range(len(lines)), key=lambda i: math.dist(centers[i], (cx, cy)))
            if math.dist(centers[near], (cx, cy)) < 150:
                hint = f" near '{lines[near]['text'][:30]}'"
        out.append(
            {
                "text": "",
                "x1": int(x1),
                "y1": int(y1),
                "x2": int(x2),
                "y2": int(y2),
                "kind": "icon",
                "editable": False,
                "label": f"icon{hint} ({_region(cx, cy, width, height)})",
            }
        )
    return _number(out)


def offer(candidates: list[dict], dead: set[str], typed: set[str]) -> list[dict]:
    """What Jev may pick: every live control to click, plus 'type into' for editable ones.

    A field already typed into is dropped: re-clicking it only moves the caret, and the
    caret blink defeats the dead-click filter.
    """
    out = []
    for item in candidates:
        if item["kind"] == "uia_text" or item["label"] in dead or (item.get("editable") and item["text"] in typed):
            continue
        out.append({**item, "action": "click"})
        if item.get("editable"):
            out.append({**item, "action": "type", "label": f"type text into {item['label']}"})
    return _number(out)


def _box_key(c: dict) -> tuple:
    return c["x1"], c["y1"], c["x2"], c["y2"], c["action"]


def screen_text(candidates: list[dict]) -> list[str]:
    """What the window currently says: static text, field contents, OCR lines."""
    # ponytail: capped at 60 entries to keep the Jev request small; text-heavy windows lose the tail.
    return [c["label"] for c in candidates if c["kind"] in ("uia_text", "text") or c.get("editable")][:60]


# ---------------------------------------------------------------- Jev

_jev: http.client.HTTPSConnection | None = None


def api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    env_file = Path(__file__).with_name(".env")  # gitignored
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "TYPESAFE_API_KEY":
                key = value.strip().strip("\"'")
    if not key:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
            key = winreg.QueryValueEx(reg, "TYPESAFE_API_KEY")[0]
        os.environ["TYPESAFE_API_KEY"] = key
    return key


def ask_jev(goal: str, offered: list[dict], history: list[str], screen: list[str]) -> tuple[dict, float]:
    """One request: which control to act on, and whether the goal is already reached."""
    body = json.dumps(
        {
            "model": JEV_MODEL,
            "state": {"goal": goal, "previous_actions": history, "screen_text": screen},
            "questions": {
                "target": {
                    "type": "choice",
                    "instructions": "Which action moves the screen toward the goal next?",
                    "criteria": {c["id"]: c["label"] for c in offered},
                },
                "done": {
                    "type": "noul",
                    "instructions": "Does the current screen show that the goal is already achieved?",
                },
            },
        }
    )
    return _post(body)


_TYPE_STEP = re.compile(r"""^type\s+(['"])(.*)\1\s+into\s+(.+?)(\s+and\s+press\s+enter)?$""", re.I | re.S)


def parse_step(step: str) -> tuple[str, str | None, bool]:
    """'type "hi" into Search and press enter' -> ("Search", "hi", True); anything else is a click target."""
    match = _TYPE_STEP.match(step.strip())
    if match:
        return match.group(3).strip(), match.group(2), bool(match.group(4))
    return re.sub(r"^click\s+", "", step.strip(), flags=re.I), None, False


def ask_targets(
    items: list[tuple[int, str, str]], offered: list[dict], screen: list[str]
) -> tuple[dict[int, dict], float]:
    """Ground (index, target, action) items in one request; Jev answers each independently."""
    questions = {}
    for index, target, action in items:
        criteria = {c["id"]: c["label"] for c in offered if c["action"] == action}
        if criteria:
            questions[f"s{index}"] = {
                "type": "choice",
                "instructions": f"Which control is meant by: {target}",
                "criteria": criteria,
            }
    if not questions:
        return {}, 0.0
    body = json.dumps({"model": JEV_MODEL, "state": {"screen_text": screen}, "questions": questions})
    answer, elapsed = _post(body)
    by_id = {c["id"]: c for c in offered}
    picks = {}
    for key, value in answer["answers"].items():
        chosen = by_id.get(str(value.get("choice")))
        if chosen is not None:
            picks[int(key[1:])] = chosen
    return picks, elapsed


def _post(body: str) -> tuple[dict, float]:
    global _jev
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    for attempt in (0, 1):
        try:
            if _jev is None:
                _jev = http.client.HTTPSConnection(JEV_HOST, timeout=20)
            t0 = time.perf_counter()
            _jev.request("POST", "/v1/systemone", body=body, headers=headers)
            response = _jev.getresponse()
            data = response.read()
            elapsed = (time.perf_counter() - t0) * 1000
        except (http.client.HTTPException, OSError):
            if _jev is not None:
                _jev.close()
            _jev = None
            if attempt:
                raise
            continue
        if response.status != 200:
            raise RuntimeError(f"Jev HTTP {response.status}: {data[:300]!r}")
        return json.loads(data), elapsed
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- per-app library


def process_of(hwnd: int) -> tuple[int, str]:
    """(pid, lowercase exe stem) of the process owning a window."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    name = ""
    if handle:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            name = Path(buffer.value).stem.lower()
        kernel32.CloseHandle(handle)
    return pid.value, name or f"pid{pid.value}"


_RISKY = re.compile(
    r"\b(delete|remove|send|discard|close|exit|quit|sign ?out|log ?out|uninstall|reset|erase|"
    r"shut ?down|restart|empty|clear|purge|archive|block|report|pay|buy|submit|publish|share)\b",
    re.I,
)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def lookup(target: str, action: str, offered: list[dict]) -> dict | None:
    """The one visible control named exactly like the step, if there is exactly one."""
    wanted = _norm(target)
    exact = [c for c in offered if c["action"] == action and _norm(c["text"]) == wanted]
    return exact[0] if len(exact) == 1 else None


class Library:
    """One app's navigation graph, persisted as JSON between runs.

    reveals: control name -> control names that appeared after clicking it (tab, menu,
             dialog, new window). Routes are chains of these from what is visible now.
    """

    def __init__(self, app: str, root: Path = LIBRARY):
        self.path = root / f"{re.sub(r'[^a-z0-9_.-]+', '_', app)}.json"
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.app = app
        self.reveals: dict[str, list[str]] = data.get("reveals", {})

    def record(self, control: str, before: set[str], after: set[str]) -> None:
        if _RISKY.search(control):  # Send, Delete...: what follows is a new state, not a menu
            return
        # ponytail: names with a digit or over 40 chars are data (dates, times, counts, mail rows),
        # not controls. Ceiling: real controls like "Heading 1" are never learned as revealed.
        appeared = sorted(n for n in after - before if len(n) <= 40 and not re.search(r"\d", n))
        # ponytail: a click counts as navigation only when >= 3 controls appear, so a click that
        # just renames one control (Calculator "Clear" -> "Clear entry") never becomes a route.
        # Ceiling: menus with only one or two entries are not learned.
        if len(appeared) >= 3:
            self.reveals[control] = appeared[:300]

    def path_to(self, visible: set[str], target: str) -> list[str] | None:
        """Shortest chain of controls to click, starting from a visible one, that reveals `target`."""
        wanted = _norm(target)
        queue = deque((c, [c]) for c in sorted(visible) if c in self.reveals)
        seen = set(visible)
        while queue:
            control, path = queue.popleft()
            for name in self.reveals[control]:
                if _norm(name) == wanted:
                    return path
                if name not in seen and name in self.reveals:
                    seen.add(name)
                    queue.append((name, path + [name]))
        return None

    def reachable(self, visible: set[str]) -> dict[str, int]:
        """Control name -> clicks needed to bring it on screen, for controls not visible now."""
        found: dict[str, int] = {}
        queue = deque((c, 1) for c in sorted(visible) if c in self.reveals)
        seen = set(visible)
        while queue:
            control, depth = queue.popleft()
            for name in self.reveals[control]:
                if name in seen:
                    continue
                seen.add(name)
                found[name] = depth
                if name in self.reveals:
                    queue.append((name, depth + 1))
        return found

    def edit(self, control: str, add: list[str] | None = None, remove: list[str] | None = None, delete: bool = False) -> None:
        """Hand-correct what clicking `control` reveals; an emptied control is dropped."""
        dropped = set(remove or [])
        names = [] if delete else [n for n in self.reveals.get(control, []) if n not in dropped]
        names += [n for n in add or [] if n not in names]
        if names:
            self.reveals[control] = names
        else:
            self.reveals.pop(control, None)

    def html(self) -> str:
        """Standalone page: every route as a collapsible tree with a filter box."""
        import html as h

        revealed = {n for names in self.reveals.values() for n in names}
        roots = [c for c in sorted(self.reveals) if c not in revealed]
        placed: set[str] = set()

        def branch(control: str, ancestors: frozenset) -> str:
            placed.add(control)
            label = h.escape(control)
            if control not in self.reveals:
                return f"<li>{label}</li>"
            if control in ancestors:
                return f"<li>{label} <i>(loop)</i></li>"
            children = "".join(branch(n, ancestors | {control}) for n in self.reveals[control])
            count = len(self.reveals[control])
            return f"<li><details><summary><b>{label}</b> <small>reveals {count}</small></summary><ul>{children}</ul></details></li>"

        trees = "".join(branch(c, frozenset()) for c in roots)
        # Controls only reachable through loops have no root; show them at top level too.
        trees += "".join(branch(c, frozenset()) for c in sorted(self.reveals) if c not in placed)
        return f"""<!doctype html><meta charset="utf-8"><title>{h.escape(self.app)} link tree</title>
<style>
body{{font:14px system-ui,sans-serif;margin:24px;max-width:1100px}} ul{{list-style:none;padding-left:18px;margin:2px 0}}
li{{margin:1px 0}} summary{{cursor:pointer}} small{{color:#777}} .hit{{background:#ffe066}}
input{{font-size:14px;padding:6px;width:320px}}
</style>
<h1>{h.escape(self.app)}</h1>
<p>{len(self.reveals)} controls with routes, {len(revealed)} controls reachable through them. Source: <code>{h.escape(str(self.path))}</code></p>
<p><input id="q" placeholder="Filter controls..." autofocus> <button onclick="all(true)">Expand all</button> <button onclick="all(false)">Collapse all</button></p>
<ul id="tree">{trees}</ul>
<script>
const all = open => document.querySelectorAll('details').forEach(d => d.open = open);
document.getElementById('q').oninput = e => {{
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll('#tree li').forEach(li => {{
    const own = (li.querySelector(':scope > details > summary b') || li).firstChild.textContent.toLowerCase();
    li.classList.toggle('hit', !!q && own.includes(q));
    li.style.display = !q || li.textContent.toLowerCase().includes(q) ? '' : 'none';
    if (q) li.querySelectorAll(':scope > details').forEach(d => d.open = true);
  }});
}};
</script>"""

    def knows(self, target: str) -> bool:
        """The graph has seen this control, so it can appear once a window finishes opening."""
        wanted = _norm(target)
        return any(_norm(n) == wanted for c, names in self.reveals.items() for n in [c, *names])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"app": self.app, "reveals": self.reveals}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)


def app_of(hwnd: int, title: str) -> tuple[int, str]:
    pid, app = process_of(hwnd)
    if app == "applicationframehost":  # every UWP app (Calculator, Settings) lives in this host
        app = title.lower()
    return pid, app


def library_for(name: str) -> Library:
    """An app's library by app name ("olk", "calculator") or by part of an open window's title."""
    saved = Library(name.lower())
    if saved.path.exists():
        return saved
    match = [(h, t) for h, t in enum_windows() if name.lower() in t.lower()]
    if not match:
        apps = sorted(p.stem for p in LIBRARY.glob("*.json"))
        raise ValueError(f"no saved graph or open window for {name!r}; saved apps: {apps}")
    return Library(app_of(*match[0])[1])


# ---------------------------------------------------------------- session


class Session:
    """One goal on one window. advance() runs until done, out of steps, or text is needed."""

    def __init__(self, window: str, goal: str, max_steps: int = 12, log: bool = True):
        match = [(h, t) for h, t in enum_windows() if window.lower() in t.lower()]
        if not match:
            raise ValueError(f"no visible window title contains {window!r}")
        self.hwnd, self.title = match[0]
        self.pid, self.app = app_of(self.hwnd, self.title)
        self.lib = Library(self.app)
        self.names: set[str] = set()  # control names visible at the last read
        self._clicked: tuple[str, set[str]] | None = None  # (control, names before) of the last click
        self.goal = goal
        self.max_steps = max_steps
        self.history: list[str] = []
        self.dead: set[str] = set()
        self.typed: set[str] = set()
        self.records: list[dict] = []
        self.frames: list[tuple] = []
        self.step = 0
        self.pending: tuple | None = None
        self.started = time.perf_counter()
        self.out = RUNS / datetime.now().strftime("%Y%m%d_%H%M%S") if log else None
        self._detector = None
        self._lang = None
        self._pool = ThreadPoolExecutor(max_workers=1)

    def _read(self, rect, shot_future) -> tuple[list[dict], Image.Image, dict]:
        t0 = time.perf_counter()
        candidates = uia_candidates(self.hwnd, rect)
        timing = {"uia_ms": round((time.perf_counter() - t0) * 1000, 1)}
        shot = shot_future.result()
        source = "uia"
        if len(candidates) < UIA_MIN_CONTROLS:
            source = "vision"
            if self._detector is None:
                from fast_detect import load_detector

                self._detector = load_detector()
                self._lang = pick_lang()
            from fast_detect import predict

            lines_future = self._pool.submit(ocr_lines, shot, self._lang)
            boxes, _scores, _infer = predict(self._detector, shot, 640)
            candidates = build_candidates(lines_future.result(), boxes.tolist(), shot.size)
        timing["source"] = source
        timing["read_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return candidates, shot, timing

    def advance(self) -> dict:
        focus(self.hwnd)
        while self.step < self.max_steps:
            self.step += 1
            t_step = time.perf_counter()
            rect, candidates, shot, timing, _ = self._look()
            offered = offer(candidates, self.dead, self.typed)
            screen = screen_text(candidates)
            answer, jev_ms = ask_jev(self.goal, offered, self.history, screen)
            done = float(answer["answers"]["done"]["noul"])
            target = answer["answers"]["target"]
            chosen = next((c for c in offered if c["id"] == str(target["choice"])), None)
            record = {
                "step": self.step,
                **timing,
                "jev_ms": round(jev_ms, 1),
                "think_ms": round((time.perf_counter() - t_step) * 1000, 1),
                "offered": len(offered),
                "screen_text": screen,
                "done_probability": round(done, 3),
                "target": chosen["label"] if chosen else None,
                "target_confidence": target.get("confidence"),
            }
            if done >= DONE_THRESHOLD:
                return self._finish("done", record, shot, offered, None)
            if chosen is None:
                return self._finish("stopped: Jev returned an unknown id", record, shot, offered, None)
            if chosen["action"] == "type":
                self.pending = (chosen, rect, shot, offered, record, t_step)
                return {
                    "status": "needs_text",
                    "field": chosen["text"],
                    "goal": self.goal,
                    "step": self.step,
                    "previous_actions": self.history,
                    "visible_controls": [c["label"] for c in candidates][:80],
                    "next": "Call provide_text with the exact text for this field.",
                }
            self._act(chosen, rect, shot, offered, record, t_step, None, False)
        return self._finish("stopped: step budget spent", None, None, None, None)

    def provide_text(self, text: str, press_enter: bool = False) -> dict:
        if self.pending is None:
            return {"status": "error", "message": "no field is waiting for text"}
        chosen, rect, shot, offered, record, t_step = self.pending
        self.pending = None
        focus(self.hwnd)
        self._act(chosen, rect, shot, offered, record, t_step, text, press_enter)
        return self.advance()

    def _follow(self) -> None:
        """Switch to a new window of the same app (compose window, dialog) once it is in front."""
        front = user32.GetForegroundWindow()
        if front and front != self.hwnd and process_of(front)[0] == self.pid:
            length = user32.GetWindowTextLengthW(front)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(front, buffer, length + 1)
            self.hwnd, self.title = front, buffer.value

    def _look(self):
        """Read the front window of the app and record what the last click revealed."""
        self._follow()
        rect = window_rect(self.hwnd)
        candidates, shot, timing = self._read(rect, self._pool.submit(grab, rect))
        offered = offer(candidates, set(), set())
        self.names = {c["text"] for c in offered}
        if self._clicked is not None:  # every read until the next action refines what it revealed
            self.lib.record(self._clicked[0], self._clicked[1], self.names)
        return rect, candidates, shot, timing, offered

    def _hop(self, control: dict, rect, shot) -> bool:
        self._clicked = (control["text"], self.names)
        click(rect[0] + (control["x1"] + control["x2"]) // 2, rect[1] + (control["y1"] + control["y2"]) // 2)
        return settle(rect, shot)[1]

    def describe(self) -> dict:
        """The current window's controls plus every control the graph knows how to reach."""
        _rect, candidates, _shot, _timing, offered = self._look()
        self.lib.save()
        return {
            "app": self.app,
            "window": self.title,
            "buttons": list(dict.fromkeys(c["text"] for c in offered if c["action"] == "click" and not c.get("editable"))),
            "fields": list(dict.fromkeys(c["text"] for c in offered if c["action"] == "type")),
            "screen_text": screen_text(candidates),
            "reachable": self.lib.reachable(self.names),
            "graph": {"routes": len(self.lib.reveals), "file": str(self.lib.path)},
        }

    def explore(self, max_clicks: int = 40) -> dict:
        """Map the app by clicking each tab (TabItem) once; tabs only switch views.

        Other controls are never clicked here: in apps like Outlook an unknown button can
        send, delete or move data. Everything else is learned from normal runs.
        """
        focus(self.hwnd)
        rect, _candidates, shot, _timing, offered = self._look()
        clicked: set[str] = set()
        while len(clicked) < max_clicks:
            todo = [
                c for c in offered
                if c["action"] == "click" and c.get("ctype") == 50019
                and c["text"] not in clicked and not _RISKY.search(c["text"])
            ]
            if not todo:
                break
            clicked.add(todo[0]["text"])
            self._hop(todo[0], rect, shot)
            rect, _candidates, shot, _timing, offered = self._look()
        self.lib.save()
        return {
            "app": self.app,
            "tabs_clicked": sorted(clicked),
            "routes": len(self.lib.reveals),
            "total_ms": round((time.perf_counter() - self.started) * 1000, 1),
            "reachable": self.lib.reachable(self.names),
        }

    def run_plan(self, steps: list[str]) -> dict:
        """Execute caller-planned steps; each step is grounded to a control as cheaply as possible.

        Order: exact control name on screen, then a walk along the app graph to a screen that
        shows the control, then Jev. Jev grounds all remaining unknown steps in one request and
        its answers are reused while the set of clickable controls stays the same. A control the
        graph has seen but cannot reach from here (a window still opening) is waited for up to 3 s.
        """
        plan = [parse_step(step) for step in steps]
        focus(self.hwnd)
        picks: dict[int, dict] = {}
        layout = None
        for index, (target, text, press_enter) in enumerate(plan):
            self.step = index + 1
            t_step = time.perf_counter()
            action = "click" if text is None else "type"
            rect, candidates, shot, timing, offered = self._look()
            chosen = lookup(target, action, offered)
            path = self.lib.path_to(self.names, target) if chosen is None else None
            # A window still opening keeps gaining controls (Outlook's compose pane): read again
            # until the name shows up or the controls stop changing; a known control gets 3 s.
            while chosen is None and not path and time.perf_counter() - t_step < 3.0:
                seen = self.names
                time.sleep(0.05)
                rect, candidates, shot, timing, offered = self._look()
                chosen = lookup(target, action, offered)
                path = self.lib.path_to(self.names, target) if chosen is None else None
                if self.names == seen and not self.lib.knows(target):
                    break
            grounded_by, jev_ms, route = "name", 0.0, []
            if chosen is None:
                for name in path or []:
                    hop = next((c for c in offered if c["action"] == "click" and c["text"] == name), None)
                    if hop is None:
                        break
                    self._hop(hop, rect, shot)
                    route.append(name)
                    rect, candidates, shot, timing, offered = self._look()
                chosen = lookup(target, action, offered) if route else None
                grounded_by = "graph"
            if chosen is None:
                grounded_by = "jev"
                by_box = {_box_key(c): c for c in offered}
                chosen = picks.get(index)
                current = by_box.get(_box_key(chosen)) if chosen is not None else None
                # Renamed-in-place controls ("Clear" -> "Clear entry") keep the layout; a new or
                # moved control (menu opened) or a renamed pick means the answers are stale.
                if frozenset(by_box) != layout or current is None or current["label"] != chosen["label"]:
                    items = [(i, t, "click" if x is None else "type") for i, (t, x, _) in enumerate(plan) if i >= index]
                    picks, jev_ms = ask_targets(items, offered, screen_text(candidates))
                    layout = frozenset(by_box)
                    current = picks.get(index)
                chosen = current
            record = {
                "step": self.step,
                **timing,
                "jev_ms": round(jev_ms, 1),
                "think_ms": round((time.perf_counter() - t_step) * 1000, 1),
                "offered": len(offered),
                "instruction": steps[index],
                "grounded_by": grounded_by,
                "route": route,
                "target": chosen["label"] if chosen else None,
            }
            if chosen is None:
                summary = self._finish(f"stopped: no control found for step {self.step} {steps[index]!r}", record, shot, offered, None)
                return self._with_screen(summary)
            self._act(chosen, rect, shot, offered, record, t_step, text, press_enter)
        return self._with_screen(self._finish("done", None, None, None, None))

    def _with_screen(self, summary: dict) -> dict:
        """Final read: links the last click into the graph and returns what the screen says."""
        summary["app"] = self.app
        self._follow()
        if user32.IsWindow(self.hwnd):
            _rect, candidates, _shot, _timing, _offered = self._look()
            summary["screen_text"] = screen_text(candidates)
        self.lib.save()
        return summary

    def _act(self, chosen, rect, shot, offered, record, t_step, text, press_enter) -> None:
        cx = rect[0] + (chosen["x1"] + chosen["x2"]) // 2
        cy = rect[1] + (chosen["y1"] + chosen["y2"]) // 2
        self._clicked = (chosen["text"], self.names) if text is None else None
        click(cx, cy)
        if text is not None:
            if set_value(cx, cy, text):
                type_text("\n" if press_enter else "")
            else:
                # Web fields re-render as they take focus (Outlook's To line opens a suggestion
                # popup) and drop keystrokes sent meanwhile.
                settle(rect, shot)
                type_text(text)
                if press_enter:
                    # Autocomplete fields (Outlook's To line) ignore Enter until their
                    # suggestions have rendered.
                    settle(rect, grab(rect))
                    type_text("\n")
            action = f"type {text!r} into {chosen['text']}"
            self.typed.add(chosen["text"])
        else:
            action = f"click {chosen['label']}"
        settle_ms, changed = settle(rect, shot)
        if text is None and not changed:
            self.dead.add(chosen["label"])
        self.history.append(action)
        record.update(action=action, settle_ms=round(settle_ms, 1), screen_changed=changed)
        if text is None:
            record["step_ms"] = round((time.perf_counter() - t_step) * 1000, 1)
        self.records.append(record)
        self.frames.append((record["step"], shot, offered, chosen))

    def _finish(self, status, record, shot, offered, chosen) -> dict:
        self.lib.save()
        if record is not None:
            record["action"] = status
            self.records.append(record)
            self.frames.append((record["step"], shot, offered, chosen))
        summary = {
            "status": status,
            "window": self.title,
            "goal": self.goal,
            "total_ms": round((time.perf_counter() - self.started) * 1000, 1),
            "steps": [
                {
                    k: r.get(k)
                    for k in (
                        "step", "source", "grounded_by", "route", "read_ms", "jev_ms", "think_ms", "settle_ms",
                        "screen_changed", "action",
                    )
                }
                for r in self.records
            ],
        }
        if self.out is not None:
            summary["log"] = str(self.out)
            final = grab(window_rect(self.hwnd))
            _log_pool.submit(self._write, final, dict(summary))
        return summary

    def _write(self, final: Image.Image, summary: dict) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        # Fast PNG compression: the logs are written while the next call may already be running.
        final.save(self.out / "final.png", compress_level=1)
        for step, shot, offered, chosen in self.frames:
            shot.save(self.out / f"step{step}_screen.png", compress_level=1)
            draw(shot, offered, chosen).save(self.out / f"step{step}_candidates.png", compress_level=1)
        records = {r["step"]: r for r in self.records}
        for step, _shot, offered, _chosen in self.frames:
            (self.out / f"step{step}.json").write_text(
                json.dumps({"record": records.get(step), "offered": offered}, indent=2), encoding="utf-8"
            )
        summary["steps"] = self.records
        (self.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        write_readme(self.out, summary)


# ---------------------------------------------------------------- logs


def font(size: int):
    for name in ("segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw(image: Image.Image, offered: list[dict], chosen: dict | None) -> Image.Image:
    marked = image.convert("RGB")
    pen = ImageDraw.Draw(marked)
    face = font(13)
    for item in offered:
        if item.get("action") == "type":
            continue
        color = {"text": "red", "icon": "deepskyblue"}.get(item["kind"], "orange")
        pen.rectangle((item["x1"], item["y1"], item["x2"], item["y2"]), outline=color, width=1)
        pen.text((item["x1"], max(0, item["y1"] - 13)), item["id"], fill=color, font=face)
    if chosen:
        box = (chosen["x1"] - 3, chosen["y1"] - 3, chosen["x2"] + 3, chosen["y2"] + 3)
        pen.rectangle(box, outline="lime", width=4)
        verb = "TYPE INTO" if chosen.get("action") == "type" else "CLICK"
        pen.text((chosen["x1"], max(0, chosen["y1"] - 28)), f"{verb} {chosen['text']}", fill="lime", font=font(20))
    return marked


def write_readme(out: Path, summary: dict) -> None:
    rows = [
        "# Agent run",
        "",
        f"Window: {summary['window']}",
        f"Goal: {summary['goal']}",
        f"Result: {summary['status']} in {summary['total_ms']} ms",
        "",
        "| Step | Source | Read UI | Jev | Start to decision | Wait for screen | Action |",
        "|---:|---|---:|---:|---:|---:|---|",
    ]
    for r in summary["steps"]:
        rows.append(
            f"| {r['step']} | {r.get('source')} | {r.get('read_ms')} | {r.get('jev_ms')} | {r.get('think_ms')} | "
            f"{r.get('settle_ms', '')} | {r.get('action')} (done p={r.get('done_probability')}) |"
        )
    rows += [
        "",
        "All times are ms. Source `uia` is one cached UI Automation read; `vision` is YOLOv9 + OCR, used only",
        "when the window exposes fewer than 8 named controls. `stepN_screen.png` is the raw window,",
        "`stepN_candidates.png` shows what Jev could pick (orange = UI Automation, red = OCR text,",
        "blue = YOLOv9 icon, green = chosen). `stepN.json` has every option Jev saw.",
    ]
    (out / "README.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- check


def _self_check() -> None:
    lines = [{"text": "Stress - von Mises", "x1": 10, "y1": 900, "x2": 150, "y2": 915}]
    boxes = [
        [5, 895, 160, 920],      # wraps the text line: dropped, the line is the target
        [12, 870, 28, 886],      # small icon just above the line: kept with a hint
        [0, 0, 1000, 1000],      # a whole pane: dropped
    ]
    got = build_candidates(lines, boxes, (1000, 1000))
    assert [c["kind"] for c in got] == ["text", "icon"], got
    assert got[0]["label"] == "Stress - von Mises (bottom-left)", got[0]
    assert got[1]["label"] == "icon near 'Stress - von Mises' (bottom-left)", got[1]
    row = [
        ("7", {"x": 10, "y": 0, "width": 8, "height": 14}),
        ("8", {"x": 80, "y": 0, "width": 8, "height": 14}),
        ("Temperature", {"x": 200, "y": 0, "width": 70, "height": 14}),
        ("(ht)", {"x": 274, "y": 0, "width": 20, "height": 14}),
    ]
    assert [s["text"] for s in split_line(row)] == ["7", "8", "Temperature (ht)"]
    field = {"text": "Search", "label": "Search (top-left) [editable, empty]", "editable": True,
             "kind": "uia", "x1": 0, "y1": 0, "x2": 9, "y2": 9}
    button = {"text": "Go", "label": "Go (top-left)", "editable": False, "kind": "uia", "x1": 0, "y1": 0, "x2": 9, "y2": 9}
    offered = offer([field, button], dead=set(), typed=set())
    assert [(c["id"], c["action"]) for c in offered] == [("c0", "click"), ("c1", "type"), ("c2", "click")]
    assert offered[1]["label"] == "type text into Search (top-left) [editable, empty]"
    assert [c["text"] for c in offer([field, button], dead=set(), typed={"Search"})] == ["Go"]
    assert offer([field, button], dead={"Go (top-left)"}, typed={"Search"}) == []
    display = {**button, "text": "Display is 42", "label": "Display is 42 (top-center)", "kind": "uia_text"}
    assert [c["text"] for c in offer([display, button], dead=set(), typed=set())] == ["Go"]
    assert screen_text([field, display, button]) == [field["label"], display["label"]]
    assert parse_step("click Seven") == ("Seven", None, False)
    assert parse_step("type \"it's 5\" into Search box and press Enter") == ("Search box", "it's 5", True)
    assert parse_step("type 'hi' into Text editor") == ("Text editor", "hi", False)
    many = [{"text": f"row {i}", "x1": 0, "y1": i * 20, "x2": 50, "y2": i * 20 + 15} for i in range(300)]
    assert len(build_candidates(many, [], (100, 6000))) == MAX_CANDIDATES
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        lib = Library("demo app", Path(root))
        assert lookup("go", "click", offered)["text"] == "Go" and lookup("Send it", "click", offered) is None
        home = {"Home", "Insert", "New mail", "Reply"}
        insert = {"Home", "Insert", "Table", "Picture", "Link"}
        lib.record("Insert", home, insert)
        lib.record("Table", insert, insert | {"Rows", "Columns", "OK"})
        lib.record("Seven", home, home | {"Clear entry"})  # one renamed control is not navigation
        assert "Seven" not in lib.reveals
        lib.record("Send", home, home | {"Reply", "Forward", "Delete", "Print"})
        lib.record("Calendar", home, home | {"1, October, 2026", "09:00 Standup", "Unread External sender " * 3})
        assert "Send" not in lib.reveals and "Calendar" not in lib.reveals
        assert lib.path_to(home, "picture") == ["Insert"]
        assert lib.path_to(home, "Columns") == ["Insert", "Table"]
        assert lib.path_to(insert, "Columns") == ["Table"]
        assert lib.path_to(home, "Reply") is None
        assert lib.reachable(home) == {"Link": 1, "Picture": 1, "Table": 1, "Columns": 2, "OK": 2, "Rows": 2}
        lib.save()
        again = Library("demo app", Path(root))
        assert again.path_to(home, "Columns") == ["Insert", "Table"]
        assert again.knows("columns") and again.knows("Insert") and not again.knows("Send it")
        again.edit("Rows", add=["Insert"])  # a loop must not recurse forever
        page = again.html()
        assert "<b>Insert</b>" in page and "Columns" in page and "(loop)" in page
        again.edit("Insert", remove=["Table"], add=["Chart"])
        assert again.reveals["Insert"] == ["Link", "Picture", "Chart"]
        again.edit("Table", remove=["Rows", "Columns", "OK"])
        again.edit("Rows", delete=True)
        assert "Table" not in again.reveals and "Rows" not in again.reveals
    assert _RISKY.search("Send") and _RISKY.search("Delete all") and not _RISKY.search("Insert")
    print("self-check ok")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", default="Calculator")
    parser.add_argument("--goal", default="Use the calculator buttons to compute 12 + 7 and show the result 19")
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    _self_check()
    if not args.check:
        session = Session(args.window, args.goal, args.steps)
        result = session.advance()
        while result["status"] == "needs_text":
            text = input(f"Text for '{result['field']}': ")
            result = session.provide_text(text)
        print(json.dumps(result, indent=2))
        _log_pool.shutdown(wait=True)
