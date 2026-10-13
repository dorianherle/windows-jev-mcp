"""Unknown-UI test: ChipCake in Chrome. Prints total, time, log, and score.

Five points: the page is open, evaporation was added, its material is Al,
spin coat was added, develop was added. No mail client is involved.
"""
import json
import os
import re
import subprocess
import sys
import time

CHECKS = ("chipcake", "evaporation", "aluminum", "spin_coat", "develop")


def judge(text: str) -> dict[str, bool]:
    low = text.lower()
    return {
        "chipcake": "chipcake" in low or "chip cake" in low,
        "evaporation": "evaporation" in low,
        # The dropdown keeps the short name "Al". "AlGaAs" and "Allow" are different controls.
        "aluminum": re.search(r"(?<![a-z0-9])al(?![a-z0-9])", low) is not None,
        "spin_coat": "spin coat" in low,
        "develop": re.search(r"\bdevelop\b", low) is not None,
    }


def _chrome() -> str:
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if os.path.isfile(path):
            return path
    raise RuntimeError("Google Chrome is not installed")


def _blob(summary: dict) -> str:
    parts = [summary.get("window") or "", summary.get("status") or ""]
    for key in ("buttons", "fields", "screen_text"):
        parts.extend(summary.get(key) or [])
    for step in summary.get("steps") or []:
        parts.append(str(step.get("action") or ""))
        parts.append(str(step.get("target") or ""))
    return "\n".join(parts)


def run() -> dict:
    import win_agent

    started = time.perf_counter()
    subprocess.Popen(
        [_chrome(), "--new-window", "--start-maximized", "--no-first-run", "--no-default-browser-check", "--force-renderer-accessibility", "https://chipcake.com/"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session = None
    summary: dict = {}
    error = ""
    deadline = time.perf_counter() + 40
    while time.perf_counter() < deadline:
        if any("chrome" in title.lower() for _hwnd, title in win_agent.enum_windows()):
            break
        time.sleep(0.5)
    else:
        error = "Chrome did not open"
    if not error:
        try:
            session = win_agent.Session("Chrome", "Add evaporation of aluminum, then spin coat, then develop", log=True)
            ready = time.perf_counter() + 50
            while time.perf_counter() < ready:
                summary = session.describe()
                if "add step" in _blob(summary).lower():
                    break
                time.sleep(1)
            summary = session.run_plan([
                "+ add step",
                "Evaporation",
                "Material: Au",
                "Al",
                "+ add step",
                "Spin coat",
                "+ add step",
                "Lithography",
                "Develop",
            ])
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if session is not None:
                try:
                    summary = session.describe()
                except Exception:
                    pass
    checks = judge(_blob(summary))
    result = {
        "total": len(CHECKS),
        "time": round((time.perf_counter() - started) * 1000, 1),
        "score": sum(checks[name] for name in CHECKS),
        "log": {
            "error": error,
            "checks": checks,
            "status": summary.get("status"),
            "window": summary.get("window"),
            "steps": summary.get("steps") or [],
            "folder": summary.get("log"),
        },
    }
    return result


def _check() -> None:
    blank = judge("")
    assert blank == {name: False for name in CHECKS}
    built = judge("ChipCake\nEvaporation\nMaterial: Al\nSpin coat\nDevelop")
    assert built == {name: True for name in CHECKS}
    assert judge("All process steps")["aluminum"] is False
    assert judge("AlGaAs\nAllow")["aluminum"] is False
    assert judge("click Al (bottom-left)")["aluminum"] is True
    assert judge("development time")["develop"] is False
    print("score-check ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
    else:
        result = run()
        text = json.dumps(result, indent=2)
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")
        open("chipcake_result.json", "w", encoding="utf-8").write(text + "\n")
