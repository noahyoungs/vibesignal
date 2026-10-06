"""Resolve per-session states into light colors and panel rows, by priority.

State validity, priority, aliases, and lifetimes live here and do not depend on
the color mapping; colors come from colors.py (user-configurable, idle = off).
"""

from __future__ import annotations

import functools
import json
import os
import time
from datetime import datetime

from . import colors, store


class State:
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    ERROR = "error"
    IDLE = "idle"
    NEEDS_INPUT = "needs_input"  # deprecated alias for BLOCKED


_ALIASES = {State.NEEDS_INPUT: State.BLOCKED}


def normalize(state: object) -> str:
    # Tolerate corrupt session files: a non-string state (e.g. a list from a
    # truncated or hand-edited file) must not crash resolution. _ALIASES.get on
    # an unhashable key would raise TypeError, so guard the type first.
    if not isinstance(state, str):
        return State.IDLE
    return _ALIASES.get(state, state)


# Higher index = higher priority when several sessions are active at once.
PRIORITY = [State.IDLE, State.WORKING, State.DONE, State.ERROR, State.BLOCKED]

# Each state has its own lifetime, because the hooks only refresh a session's
# timestamp when something happens. `working` refreshes on every tool call, so a
# working session silent past its TTL is treated as dead. `done` is a transient
# "your move" pulse that fades quickly. Errors fade after five minutes.
# Blocked prompts use a longer backstop:
# nothing refreshes them while they wait on you, so a short TTL would make a
# long-pending prompt vanish exactly when it is most overdue. They persist for a
# long backstop instead, cleared sooner when you act (state changes), when
# SessionEnd fires, or by a manual clear; the backstop only self-cleans a
# hard-crashed session that left no final event.
WORKING_TTL_SECONDS = store.DEFAULT_TTL_SECONDS  # 600 (10 min): silent working is stale
DONE_TTL_SECONDS = 90.0                          # transient "your move" pulse
BLOCKED_TTL_SECONDS = 8 * 60 * 60.0              # 28800 (8 h): needs-you spans a workday
ERROR_TTL_SECONDS = 5 * 60.0                     # 300 (5 min): transient error alert

_STATE_TTL_SECONDS: dict[str, float] = {
    State.WORKING: WORKING_TTL_SECONDS,
    State.DONE: DONE_TTL_SECONDS,
    State.BLOCKED: BLOCKED_TTL_SECONDS,
    State.ERROR: ERROR_TTL_SECONDS,
}

# Load every file that could still be live under the longest per-state TTL, then
# let _expired apply the per-state rule. Loading only DEFAULT_TTL (the working
# TTL) would drop a long-blocked session before the resolver could keep it.
_LOAD_HORIZON_SECONDS = max(_STATE_TTL_SECONDS.values())


def _expired(state: str, ts: object, now: float) -> bool:
    """True if a state has outlived its lifetime and should drop off the panel."""
    ttl = _STATE_TTL_SECONDS.get(state, WORKING_TTL_SECONDS)
    age = now - ts if isinstance(ts, (int, float)) else 0.0
    return age > ttl


# Claude Code fires no hook when you deny a permission prompt (or interrupt at
# it): no Stop, no PostToolUse, no PermissionDenied (that one is auto-mode only).
# The turn simply ends, and only the session transcript records it, as a
# `system`/`turn_duration` entry. So a blocked session whose transcript shows a
# turn ending after the block is treated as done from that moment. A prompt that
# is still pending has no such entry and stays blocked.
_TRANSCRIPT_TAIL_BYTES = 64 * 1024


def _iso_to_epoch(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@functools.lru_cache(maxsize=64)
def _last_turn_end(path: str, _mtime_ns: int, size: int) -> float | None:
    """Time of the newest turn end in the transcript tail (cached per file version)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", "ignore")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if '"turn_duration"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") == "system" and entry.get("subtype") == "turn_duration":
            return _iso_to_epoch(entry.get("timestamp"))
    return None


def _turn_ended_after(transcript: object, since: float) -> float | None:
    if not isinstance(transcript, str):
        return None
    try:
        stat = os.stat(transcript)
    except OSError:
        return None
    if stat.st_mtime < since:  # untouched since the block: still pending
        return None
    ended = _last_turn_end(transcript, stat.st_mtime_ns, stat.st_size)
    return ended if ended is not None and ended > since else None


def _session_state(d: dict) -> tuple[str, object]:
    """A session's effective (state, ts), resolving a block whose turn ended."""
    st = normalize(d.get("state", State.IDLE))
    ts = d.get("ts", 0)
    if st == State.BLOCKED and isinstance(ts, (int, float)):
        ended = _turn_ended_after(d.get("transcript"), ts)
        if ended is not None:
            return State.DONE, ended
    return st, ts


def aggregate(states: list[object]) -> str:
    best = State.IDLE
    best_rank = PRIORITY.index(State.IDLE)
    for s in states:
        s = normalize(s)
        if s not in PRIORITY:
            continue
        rank = PRIORITY.index(s)
        if rank > best_rank:
            best, best_rank = s, rank
    return best


def resolve_color(ttl: float = _LOAD_HORIZON_SECONDS,
                  now: float | None = None) -> tuple[str, list | None]:
    if now is None:
        now = time.time()
    states = []
    for d in store.load_active(ttl=ttl, now=now):
        st, ts = _session_state(d)
        if _expired(st, ts, now):
            continue
        states.append(st)
    state = aggregate(states)
    return state, colors.load_rgb()[state]


def resolve_per_session(ttl: float = _LOAD_HORIZON_SECONDS,
                        now: float | None = None) -> list[dict]:
    if now is None:
        now = time.time()
    rows = []
    rgb = colors.load_rgb()
    for d in store.load_active(ttl=ttl, now=now):
        st, ts = _session_state(d)
        if st not in PRIORITY:
            st = State.IDLE
        if _expired(st, ts, now):
            continue
        rows.append({
            "agent": d.get("agent", "?"),
            "project": d.get("project") or "?",
            "session": d.get("session", "?"),
            "state": st,
            "color": rgb[st],
            "ts": ts,
        })
    # Highest priority first (blocked at top); within a state, oldest first.
    rows.sort(key=lambda r: (-PRIORITY.index(r["state"]), r["ts"]))
    return rows
