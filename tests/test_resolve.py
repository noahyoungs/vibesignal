import json

from vibesignal import store
from vibesignal.resolve import (
    BLOCKED_TTL_SECONDS,
    DONE_TTL_SECONDS,
    ERROR_TTL_SECONDS,
    State,
    WORKING_TTL_SECONDS,
    aggregate,
    normalize,
    resolve_color,
    resolve_per_session,
)


def test_normalize_needs_input_to_blocked():
    assert normalize(State.NEEDS_INPUT) == State.BLOCKED
    assert normalize(State.WORKING) == State.WORKING


def test_blocked_beats_working_and_done():
    assert aggregate([State.WORKING, State.DONE, State.BLOCKED]) == State.BLOCKED


def test_error_beats_done_and_working():
    assert aggregate([State.WORKING, State.DONE, State.ERROR]) == State.ERROR


def test_blocked_beats_error():
    assert aggregate([State.ERROR, State.BLOCKED]) == State.BLOCKED


def test_done_beats_working():
    assert aggregate([State.WORKING, State.DONE]) == State.DONE


def test_needs_input_alias_aggregates_as_blocked():
    assert aggregate([State.WORKING, State.NEEDS_INPUT]) == State.BLOCKED


def test_empty_is_idle():
    assert aggregate([]) == State.IDLE


def test_unknown_states_ignored():
    assert aggregate(["bogus", State.WORKING]) == State.WORKING


def test_colors_cover_all_states():
    from vibesignal import colors
    rgb = colors.load_rgb()
    for s in (State.WORKING, State.BLOCKED, State.DONE, State.ERROR, State.IDLE):
        assert s in rgb
    assert rgb[State.IDLE] is None
    assert rgb[State.BLOCKED] == [220, 38, 38]
    assert rgb[State.DONE] == [0, 90, 255]


def test_configurable_states_are_exactly_the_active_priority_states():
    from vibesignal import colors
    from vibesignal.resolve import PRIORITY
    assert set(colors.ACTIVE_STATES) == set(PRIORITY) - {State.IDLE}


def test_custom_colors_change_color_but_not_priority_or_ttl(tmp_path, monkeypatch):
    from vibesignal import colors
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    colors.apply_theme("cool")
    store.record("claude", "s1", "working", project="p", now=1000.0)
    store.record("codex", "s2", "needs_input", project="p", now=1000.0)
    store.record("opencode", "s3", "done", project="p", now=1000.0)
    state, color = resolve_color(now=1001.0)
    assert state == State.BLOCKED  # alias + priority unchanged
    assert color == colors.hex_to_rgb(colors.THEMES["cool"]["blocked"])
    rows = resolve_per_session(now=1001.0)
    assert [r["state"] for r in rows] == ["blocked", "done", "working"]
    assert rows[2]["color"] == colors.hex_to_rgb(colors.THEMES["cool"]["working"])
    # done still fades at its own TTL regardless of the palette.
    later = resolve_per_session(now=1000.0 + DONE_TTL_SECONDS + 1)
    assert [r["state"] for r in later] == ["blocked", "working"]


def test_per_session_orders_blocked_first(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.record("claude", "s1", "working", project="p1", now=1000.0)
    store.record("codex", "s2", "blocked", project="p2", now=1001.0)
    store.record("claude", "s3", "done", project="p3", now=1002.0)
    rows = resolve_per_session(ttl=600, now=1050.0)  # within the done window
    assert [r["state"] for r in rows] == ["blocked", "done", "working"]
    assert rows[0]["project"] == "p2"
    assert rows[0]["color"] == [220, 38, 38]


def test_per_session_normalizes_needs_input(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.record("claude", "s1", "needs_input", project="p1", now=1000.0)
    rows = resolve_per_session(ttl=600, now=1100.0)
    assert len(rows) == 1
    assert rows[0]["state"] == "blocked"
    assert rows[0]["agent"] == "claude"


def test_normalize_ignores_non_string_state():
    # A corrupt session file may hold a non-string state; it must map to idle,
    # never raise. _ALIASES.get on an unhashable key (a list) would otherwise crash.
    assert normalize(["oops"]) == State.IDLE
    assert normalize(None) == State.IDLE
    assert normalize(42) == State.IDLE


def test_aggregate_tolerates_non_string_state():
    assert aggregate([["oops"], State.WORKING]) == State.WORKING
    assert aggregate([{"k": "v"}]) == State.IDLE


def test_per_session_tolerates_malformed_state(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    # Hand-write a corrupt but active file whose state is a list, not a string.
    state_dir = store.state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "corrupt.json").write_text(
        json.dumps({"agent": "claude", "session": "s", "state": ["oops"], "ts": 1000.0})
    )
    rows = resolve_per_session(ttl=600, now=1100.0)
    assert len(rows) == 1
    assert rows[0]["state"] == "idle"
    assert rows[0]["color"] is None


def test_done_fades_after_its_short_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.record("codex", "s1", "done", project="p", now=1000.0)
    # Within the done window: still shown.
    assert len(resolve_per_session(ttl=600, now=1000.0 + 60)) == 1
    # Past the done window: gone, so a finished/closed session does not linger.
    assert resolve_per_session(ttl=600, now=1000.0 + 120) == []


def test_blocked_and_working_persist_past_done_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.record("claude", "s1", "blocked", project="p1", now=1000.0)
    store.record("claude", "s2", "working", project="p2", now=1000.0)
    rows = resolve_per_session(ttl=600, now=1000.0 + 300)  # 5 minutes later
    assert {r["state"] for r in rows} == {"blocked", "working"}


def test_resolve_color_drops_expired_done(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.record("codex", "s1", "done", project="p", now=1000.0)
    state, color = resolve_color(ttl=600, now=1000.0 + 120)
    assert state == "idle"
    assert color is None


def test_blocked_persists_past_store_ttl(tmp_path, monkeypatch):
    # Regression: a blocked session that sits longer than the store TTL (no hook
    # refreshes its ts while it waits on you) must NOT vanish from the panel.
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.record("claude", "s1", "blocked", project="p", now=1000.0)
    later = 1000.0 + store.DEFAULT_TTL_SECONDS + 60  # ~11 min, well past the store TTL
    rows = resolve_per_session(now=later)
    assert [r["state"] for r in rows] == ["blocked"]
    # The single light also stays amber, not off.
    assert resolve_color(now=later) == ("blocked", [220, 38, 38])


def test_blocked_clears_after_backstop(tmp_path, monkeypatch):
    # The backstop still self-cleans a hard-crashed blocked session (no SessionEnd).
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.record("claude", "s1", "blocked", project="p", now=1000.0)
    assert resolve_per_session(now=1000.0 + BLOCKED_TTL_SECONDS + 1) == []


def test_working_drops_after_its_ttl(tmp_path, monkeypatch):
    # A working session that stops emitting (no tool calls) past its TTL is stale.
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.record("claude", "s1", "working", project="p", now=1000.0)
    assert resolve_per_session(now=1000.0 + WORKING_TTL_SECONDS + 1) == []


def test_error_expires_after_five_minutes(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.record("claude", "s1", "error", project="p", now=1000.0)
    store.record("opencode", "s2", "working", project="p", now=1000.0)
    assert ERROR_TTL_SECONDS == 300
    assert resolve_color(now=1299.0)[0] == State.ERROR
    assert resolve_color(now=1301.0)[0] == State.WORKING
    assert [r["state"] for r in resolve_per_session(now=1301.0)] == ["working"]


def test_done_visible_at_exact_ttl_boundary(tmp_path, monkeypatch):
    # The cutoff is `age > DONE_TTL_SECONDS`, so a done exactly at the boundary is
    # still shown; one second past it, it fades. This pins the > vs >= choice.
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.record("codex", "s1", "done", project="p", now=1000.0)
    at_boundary = resolve_per_session(ttl=600, now=1000.0 + DONE_TTL_SECONDS)
    assert [r["state"] for r in at_boundary] == ["done"]
    assert resolve_per_session(ttl=600, now=1000.0 + DONE_TTL_SECONDS + 1) == []
