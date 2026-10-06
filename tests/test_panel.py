from vibesignal import panel


def test_render_empty():
    out = panel.render([], now=1000.0, color=False)
    assert "no active sessions" in out


def test_render_lists_sessions_blocked_first():
    rows = [
        {"agent": "codex", "project": "aegis", "session": "s2",
         "state": "blocked", "color": [220, 38, 38], "ts": 1000.0},
        {"agent": "claude", "project": "random", "session": "s1",
         "state": "working", "color": [0, 200, 60], "ts": 1050.0},
    ]
    out = panel.render(rows, now=1072.0, color=False)
    assert "aegis" in out and "random" in out
    assert "blocked" in out and "working" in out
    assert out.index("aegis") < out.index("random")


def test_render_age_only_for_non_working():
    rows = [
        {"agent": "claude", "project": "p", "session": "s",
         "state": "blocked", "color": [220, 38, 38], "ts": 1000.0},
    ]
    out = panel.render(rows, now=1072.0, color=False)
    assert "1m12s" in out


def test_fmt_age():
    assert panel._fmt_age(5) == "5s"
    assert panel._fmt_age(72) == "1m12s"
    assert panel._fmt_age(3700) == "1h01m"


def test_render_colors_blocked_and_done_distinctly():
    rows = [
        {"agent": "claude", "project": "aegis", "session": "s1",
         "state": "blocked", "color": [220, 38, 38], "ts": 1000.0},
        {"agent": "claude", "project": "iet", "session": "s2",
         "state": "done", "color": [0, 90, 255], "ts": 1001.0},
    ]
    out = panel.render(rows, now=1002.0, color=True)
    # Lock red-blocked / blue-done ANSI so the panel matches the light's hues.
    assert panel._ANSI["blocked"] == "\033[31m"
    assert panel._ANSI["done"] == "\033[34m"
    assert panel._ANSI["blocked"] in out
    assert panel._ANSI["done"] in out
    # The blocked row is painted and ordered before the done row.
    assert out.index("aegis") < out.index("iet")
    assert out.index(panel._ANSI["blocked"]) < out.index(panel._ANSI["done"])
