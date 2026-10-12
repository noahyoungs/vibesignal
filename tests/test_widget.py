import sys

import pytest

from vibesignal import widget


def test_font_family_per_platform(monkeypatch):
    # Locks the Windows return value so a future helper edit cannot silently
    # swap Segoe UI for a generic family and degrade the widget's look there.
    monkeypatch.setattr("sys.platform", "win32")
    assert widget._font_family() == "Segoe UI"
    monkeypatch.setattr("sys.platform", "darwin")
    assert widget._font_family() == "Helvetica Neue"
    monkeypatch.setattr("sys.platform", "linux")
    assert widget._font_family() == "DejaVu Sans"


def test_glyph_idle_vs_active():
    assert widget.glyph("idle") == "○"
    assert widget.glyph("blocked") == "●"
    assert widget.glyph("working") == "●"
    assert widget.glyph("done") == "●"


def test_state_hex_uses_resolved_color_and_grey_for_idle():
    assert widget.state_hex([220, 38, 38]) == "#dc2626"
    assert widget.state_hex(None) == widget._IDLE


def test_alarm_palette_follows_configured_color_and_stays_legible():
    dark = widget._palette("blocked", [220, 38, 38])
    assert dark["alarm"] and dark["frame"] == "#dc2626" and dark["header_fg"] == "#ffffff"
    light = widget._palette("blocked", [232, 192, 0])  # warm-theme gold
    assert light["header_fg"] == widget._HEADER  # dark text on a light header
    assert widget._palette("working", [0, 200, 60]) is widget._CALM


def test_row_fields_blocked():
    row = {"project": "aegis", "agent": "claude", "state": "blocked", "ts": 1000.0}
    g, project, agent, state, age = widget.row_fields(row, now=1072.0)
    assert g == "●"
    assert project == "aegis"
    assert agent == "claude"
    assert state == "blocked"
    assert age == "1m12s"


def test_row_fields_working_hides_age():
    row = {"project": "random", "agent": "codex", "state": "working", "ts": 1000.0}
    _g, _project, _agent, _state, age = widget.row_fields(row, now=1072.0)
    assert age == "—"  # em dash, not an age


def test_row_fields_truncates_long_project():
    row = {"project": "a-really-long-project-name", "agent": "claude",
           "state": "done", "ts": 1000.0}
    _g, project, _a, _s, _age = widget.row_fields(row, now=1000.0)
    assert len(project) <= 18


def test_widget_constructs_and_renders_one_tick(monkeypatch):
    # Guarded construction smoke test: skips on a headless box (no Tk display) and
    # otherwise checks that __init__'s one synchronous _tick renders a store row
    # without raising. destroy() cancels the pending after() callbacks, so nothing
    # schedules past the test.
    tk = pytest.importorskip("tkinter")
    from vibesignal import resolve
    row = {"agent": "claude", "project": "random", "session": "s1",
           "state": "blocked", "color": [220, 38, 38], "ts": 1000.0}
    monkeypatch.setattr(resolve, "resolve_per_session", lambda *a, **k: [row])
    monkeypatch.setattr(resolve, "resolve_color", lambda *a, **k: ("blocked", [220, 38, 38]))
    try:
        w = widget.Widget(interval_ms=10_000)
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    try:
        w.root.update_idletasks()
        # Prove the monkeypatched row rendered, not the "no active sessions"
        # placeholder (which also appends one cell): the row path emits the
        # project, agent, and state as separate labels. The accent-bar Frame has
        # no "text" option, so filter on widgets that carry one.
        texts = [c.cget("text") for c in w._cells if "text" in c.keys()]
        assert "random" in texts   # project
        assert "claude" in texts   # agent
        assert "blocked" in texts  # state
        # Whole-panel alarm: a blocked aggregate turns the frame red.
        assert str(w.root.cget("bg")) == "#dc2626"
    finally:
        w.root.destroy()


def test_keep_on_top_runs_each_tick(monkeypatch):
    # The macOS always-on-top fix hinges on re-asserting topmost every tick, so
    # lock in that _tick calls _keep_on_top (the synchronous first tick counts).
    tk = pytest.importorskip("tkinter")
    from vibesignal import resolve
    monkeypatch.setattr(resolve, "resolve_per_session", lambda *a, **k: [])
    monkeypatch.setattr(resolve, "resolve_color", lambda *a, **k: ("idle", None))
    calls = {"n": 0}
    monkeypatch.setattr(widget.Widget, "_keep_on_top",
                        lambda self: calls.__setitem__("n", calls["n"] + 1))
    try:
        w = widget.Widget(interval_ms=10_000)
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    try:
        assert calls["n"] >= 1
    finally:
        w.root.destroy()


def test_macos_float_level_silent_without_pyobjc(monkeypatch):
    # On a machine without the `macos` extra, the native level-raise must be a
    # no-op that never raises, so the stdlib -topmost path is the only effect.
    tk = pytest.importorskip("tkinter")
    from vibesignal import resolve
    monkeypatch.setattr(resolve, "resolve_per_session", lambda *a, **k: [])
    monkeypatch.setattr(resolve, "resolve_color", lambda *a, **k: ("idle", None))
    try:
        w = widget.Widget(interval_ms=10_000)
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    try:
        w._macos_float_level()  # must not raise regardless of pyobjc presence
    finally:
        w.root.destroy()


def test_keep_on_top_noop_on_linux(monkeypatch):
    # On Linux the window manager holds the startup -topmost, so _keep_on_top must
    # do nothing: neither the macOS float path nor the Windows pin runs (a per-tick
    # re-raise would be needless z-order churn). macOS and Windows DO re-assert --
    # see their own tests below.
    tk = pytest.importorskip("tkinter")
    from vibesignal import resolve
    monkeypatch.setattr(resolve, "resolve_per_session", lambda *a, **k: [])
    monkeypatch.setattr(resolve, "resolve_color", lambda *a, **k: ("idle", None))
    try:
        w = widget.Widget(interval_ms=10_000)
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    try:
        monkeypatch.setattr("sys.platform", "linux")
        called = {"mac": 0, "win": 0}
        monkeypatch.setattr(w, "_macos_float_level",
                            lambda: called.__setitem__("mac", called["mac"] + 1) or True)
        monkeypatch.setattr(w, "_windows_pin_topmost",
                            lambda: called.__setitem__("win", called["win"] + 1) or True)
        w._keep_on_top()
        assert called == {"mac": 0, "win": 0}
    finally:
        w.root.destroy()


def test_keep_on_top_reasserts_topmost_on_windows(monkeypatch):
    # The Windows fix: a one-time startup -topmost gets buried when another window
    # later becomes topmost, so _keep_on_top must re-pin every tick. Verify the
    # win32 branch calls the native pin and not the macOS float path.
    tk = pytest.importorskip("tkinter")
    from vibesignal import resolve
    monkeypatch.setattr(resolve, "resolve_per_session", lambda *a, **k: [])
    monkeypatch.setattr(resolve, "resolve_color", lambda *a, **k: ("idle", None))
    try:
        w = widget.Widget(interval_ms=10_000)
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    try:
        monkeypatch.setattr("sys.platform", "win32")
        calls = {"win": 0, "mac": 0}
        monkeypatch.setattr(w, "_windows_pin_topmost",
                            lambda: calls.__setitem__("win", calls["win"] + 1) or True)
        monkeypatch.setattr(w, "_macos_float_level",
                            lambda: calls.__setitem__("mac", calls["mac"] + 1) or True)
        w._keep_on_top()
        assert calls["win"] == 1
        assert calls["mac"] == 0
    finally:
        w.root.destroy()


def test_keep_on_top_windows_falls_back_to_tk_when_pin_fails(monkeypatch):
    # If the native SetWindowPos pin fails (returns False), the win32 branch must
    # fall back to the stdlib -topmost + lift() re-assert rather than give up.
    tk = pytest.importorskip("tkinter")
    from vibesignal import resolve
    monkeypatch.setattr(resolve, "resolve_per_session", lambda *a, **k: [])
    monkeypatch.setattr(resolve, "resolve_color", lambda *a, **k: ("idle", None))
    try:
        w = widget.Widget(interval_ms=10_000)
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    try:
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(w, "_windows_pin_topmost", lambda: False)
        fell_back = {"n": 0}
        monkeypatch.setattr(w, "_topmost_reassert_tk",
                            lambda: fell_back.__setitem__("n", fell_back["n"] + 1))
        w._keep_on_top()
        assert fell_back["n"] == 1
    finally:
        w.root.destroy()


def test_windows_pin_topmost_safe_off_windows(monkeypatch):
    # _windows_pin_topmost must never raise: on macOS / Linux CI ctypes.windll is
    # absent, so it returns False cleanly; on Windows it returns a bool. This keeps
    # _keep_on_top callable on every platform without a guard at the call site.
    tk = pytest.importorskip("tkinter")
    from vibesignal import resolve
    monkeypatch.setattr(resolve, "resolve_per_session", lambda *a, **k: [])
    monkeypatch.setattr(resolve, "resolve_color", lambda *a, **k: ("idle", None))
    try:
        w = widget.Widget(interval_ms=10_000)
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    try:
        result = w._windows_pin_topmost()
        assert isinstance(result, bool)
        if sys.platform != "win32":
            assert result is False
    finally:
        w.root.destroy()


def test_windows_pin_topmost_repins_after_demotion():
    # Real-window proof (Windows only, needs a display) of the topmost-BIT-loss mode:
    # after the widget is demoted out of the topmost band -- simulated by forcing
    # WS_EX_TOPMOST off, as a shell or display change can do -- _windows_pin_topmost
    # must restore the bit. The distinct within-band burial mode (another topmost
    # window sitting above ours while ours keeps its bit) is covered by
    # test_windows_pin_topmost_raises_above_competing_topmost below. Exercises the
    # real SetWindowPos path, not a mock.
    if sys.platform != "win32":
        pytest.skip("Windows-only: exercises the Win32 SetWindowPos topmost pin")
    tk = pytest.importorskip("tkinter")
    import ctypes
    from ctypes import wintypes
    try:
        w = widget.Widget(interval_ms=10_000)
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    try:
        w.root.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        GWL_EXSTYLE, WS_EX_TOPMOST, GA_ROOT = -20, 0x0008, 2
        hwnd = user32.GetAncestor(w.root.winfo_id(), GA_ROOT) or w.root.winfo_id()
        # Demote: clear WS_EX_TOPMOST, as if another window took over the band.
        HWND_NOTOPMOST = wintypes.HWND(-2)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        assert not (user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
        # Re-pin via the code under test, then confirm topmost is back on.
        assert w._windows_pin_topmost() is True
        assert user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST
    finally:
        w.root.destroy()


def test_windows_pin_topmost_raises_above_competing_topmost():
    # Real-window proof (Windows only, needs a display) of the ACTUAL reported bug:
    # another topmost window sits above the widget while the widget keeps its topmost
    # bit (within-band burial, not a cleared bit). Put a competing topmost window
    # above the widget, then confirm _windows_pin_topmost raises the widget back on
    # top. Z-order is read from EnumWindows, which returns top-level windows
    # top-to-bottom, so "above" means "appears earlier in the enumeration".
    if sys.platform != "win32":
        pytest.skip("Windows-only: exercises the Win32 SetWindowPos topmost pin")
    tk = pytest.importorskip("tkinter")
    import ctypes
    from ctypes import wintypes
    try:
        w = widget.Widget(interval_ms=10_000)
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    comp = None
    try:
        w.root.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        GA_ROOT = 2

        def root_hwnd(win):
            return user32.GetAncestor(win.winfo_id(), GA_ROOT) or win.winfo_id()

        def zorder_index(target):
            # Position of target in the system top-level Z-order (0 == topmost).
            order = []
            proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            cb = proto(lambda hwnd, _l: (order.append(hwnd) or True))
            user32.EnumWindows(cb, 0)
            return order.index(target) if target in order else None

        # A competing borderless topmost window, then force it above the widget.
        comp = tk.Toplevel(w.root)
        comp.overrideredirect(True)
        comp.attributes("-topmost", True)
        comp.geometry("120x60+40+40")
        comp.update_idletasks()
        widget_hwnd, comp_hwnd = root_hwnd(w.root), root_hwnd(comp)
        HWND_TOPMOST = wintypes.HWND(-1)
        SWP = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
        user32.SetWindowPos(comp_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP)

        w_i, c_i = zorder_index(widget_hwnd), zorder_index(comp_hwnd)
        assert w_i is not None and c_i is not None
        assert c_i < w_i  # competitor starts above the widget

        # The code under test must raise the widget back above the competitor.
        assert w._windows_pin_topmost() is True
        w.root.update_idletasks()
        assert zorder_index(widget_hwnd) < zorder_index(comp_hwnd)
    finally:
        if comp is not None:
            comp.destroy()
        w.root.destroy()
