"""Live multi-session panel: a foreground TUI over the vibesignal store.

Renders one row per active session so the user can see which of several
concurrent agent sessions needs attention. Reads the same store the light uses,
so it covers Claude and Codex together and stays in sync with the light.
"""

from __future__ import annotations

import sys
import time

from . import resolve

_RESET = "\033[0m"
# Basic 16-color ANSI approximating the default (calm) hues. Not tied to the
# configured mapping: 24-bit color is not reliable across terminals, and the glyph
# and state name already carry the meaning. The bright variants read softer than
# the plain ones in most terminal themes.
_ANSI = {
    "working": "\033[36m",  # cyan (ocean teal)
    "blocked": "\033[91m",  # bright red (dawn rose)
    "done": "\033[94m",     # bright blue (night periwinkle)
    "error": "\033[95m",    # bright magenta (night orchid)
    "idle": "\033[90m",
}
_GLYPH = {"working": ".", "blocked": "*", "done": "o", "error": "x", "idle": "-"}


def _fmt_age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def render(rows: list[dict], now: float, color: bool = True) -> str:
    def paint(text: str, state: str) -> str:
        if not color:
            return text
        return f"{_ANSI.get(state, '')}{text}{_RESET}"

    lines = [f"  {'PROJECT':<16} {'AGENT':<7} {'STATE':<9} FOR"]
    if not rows:
        lines.append("  (no active sessions)")
        return "\n".join(lines)
    for r in rows:
        st = r.get("state", "idle")
        glyph = _GLYPH.get(st, " ")
        project = str(r.get("project") or "?")[:16]
        agent = str(r.get("agent") or "?")[:7]
        age = "--" if st == "working" else _fmt_age(now - r.get("ts", now))
        body = f"{glyph} {project:<16} {agent:<7} {glyph} {st:<7} {age}"
        lines.append(paint(body, st))
    return "\n".join(lines)


def watch(interval: float = 1.0, once: bool = False) -> int:
    try:
        while True:
            rows = resolve.resolve_per_session()
            now = time.time()
            sys.stdout.write("\033[2J\033[H")  # clear screen, home cursor
            sys.stdout.write(render(rows, now) + "\n")
            sys.stdout.flush()
            if once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stdout.write(_RESET + "\n")
        sys.stdout.flush()
        return 0
