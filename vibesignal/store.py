"""Per-session state storage with TTL-based staleness.

State lives in a directory of small JSON files, one per (agent, session). Hooks
need no long-running process: every hook invocation reads the directory,
recomputes, and writes back. Viewers and the optional Matter worker only read it. Sessions that stop emitting events are dropped after the TTL, so a
crashed session cannot pin the light to a stale color.

Writes are atomic (temp file plus os.replace) so a concurrent reader never sees a
half-written file. The event read-modify-write is additionally serialized by a
cross-process lock (see lock.py): atomic writes close torn reads, the lock closes
the race where a lower-priority writer overwrites a newer needs_input signal.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

DEFAULT_TTL_SECONDS = 600  # 10 minutes


def base_dir() -> Path:
    override = os.environ.get("VIBECODING_SIGNAL_DIR")
    return Path(override) if override else Path.home() / ".vibesignal"


def state_dir() -> Path:
    sessions = base_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    return sessions


def lock_path() -> Path:
    return base_dir() / "lock"


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically. Temp file uses a non-.json suffix so it is
    never picked up by the load_active glob while a write is in flight."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _session_path(agent: str, session: str) -> Path:
    # Hash an unambiguous (agent, session) tuple so distinct ids never collide,
    # even after the readable prefix is sanitized (e.g. "a/b" vs "a_b" both
    # sanitize to "a_b" but hash differently).
    digest = hashlib.sha256(f"{agent}\0{session}".encode("utf-8")).hexdigest()[:12]
    readable = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{agent}-{session}")[:40]
    return state_dir() / f"{readable}-{digest}.json"


def record(agent: str, session: str, state: str, project: str | None = None,
           now: float | None = None, transcript: str | None = None) -> None:
    now = time.time() if now is None else now
    payload = {
        "agent": agent,
        "session": session,
        "state": state,
        "project": project,
        "ts": now,
    }
    if transcript:
        payload["transcript"] = transcript
    _atomic_write(_session_path(agent, session), json.dumps(payload))


def read_session(agent: str, session: str) -> dict | None:
    return _read_state_file(_session_path(agent, session))


def _read_state_file(path: Path) -> dict | None:
    """Read one session file, returning a dict or None. Tolerates a missing file,
    invalid JSON, or valid JSON of the wrong shape (e.g. a list), so a single
    corrupt file cannot break resolution for every later hook call."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def load_active(ttl: float = DEFAULT_TTL_SECONDS, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    active: list[dict] = []
    for path in state_dir().glob("*.json"):
        data = _read_state_file(path)
        if data is None:
            continue
        ts = data.get("ts", 0)
        if not isinstance(ts, (int, float)):
            continue
        if now - ts > ttl:
            continue
        active.append(data)
    return active


def clear(agent: str | None = None, session: str | None = None) -> None:
    if agent is None and session is None:
        for path in state_dir().glob("*.json"):
            path.unlink(missing_ok=True)
        return
    # Scoped clear: match on whichever of agent/session was given, so a partial
    # filter like `clear --agent claude` does not wipe other agents' sessions.
    for path in state_dir().glob("*.json"):
        data = _read_state_file(path)
        if data is None:
            continue
        if agent is not None and data.get("agent") != agent:
            continue
        if session is not None and data.get("session") != session:
            continue
        path.unlink(missing_ok=True)


def _last_color_path() -> Path:
    return base_dir() / "last_color.json"


def get_last_color() -> list | None:
    try:
        data = json.loads(_last_color_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("color")


def set_last_color(color: list | None) -> None:
    _atomic_write(_last_color_path(), json.dumps({"color": color}))


def _matter_status_path() -> Path:
    return base_dir() / "matter_status.json"


def get_matter_status() -> dict:
    try:
        data = json.loads(_matter_status_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"color": None, "error": None}
    return data if isinstance(data, dict) else {"color": None, "error": None}


def _set_matter_status(**updates) -> None:
    status = get_matter_status()
    status.update(updates)
    status["ts"] = time.time()
    _atomic_write(_matter_status_path(), json.dumps(status))


def set_matter_last_color(color: list | None) -> None:
    _set_matter_status(color=color)


def set_matter_error(error: str | None) -> None:
    _set_matter_status(error=error)
