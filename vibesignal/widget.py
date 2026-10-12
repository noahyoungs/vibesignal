"""Always-on-top floating panel (Tkinter, standard library) over the store.

A small borderless window that stays on top and shows one row per active
session, blocked first, refreshed about once a second. It is the desktop-GUI
sibling of panel.py (the terminal table) and the future physical light: all
three read the same store through resolve_per_session(), so this adds a
renderer without touching the hook layer.

Run with:
  Windows:        pythonw -m vibesignal widget    (no console window)
  macOS / Linux:  vibesignal widget &        (background the GUI)

Drag by the header; right-click to quit. On macOS, Control-click works too
because some Tk builds report the right mouse button as Button-2 rather than
Button-3, and Control-click is the historical single-button right-click chord.
"""
from __future__ import annotations

import sys
import time
import tkinter as tk
import tkinter.font as tkfont

from . import colors, resolve
from .panel import _fmt_age


def _font_family() -> str:
    """Per-platform UI font family.

    Tk silently substitutes when a family is missing, but the substitute is
    often a poor visual match (Times on macOS, Courier on bare X). Naming the
    platform-native family up front keeps the panel legible without forcing a
    runtime font-list scan.
    """
    if sys.platform == "darwin":
        return "Helvetica Neue"  # ships with every macOS; SF Pro is unreliable via Tk
    if sys.platform == "win32":
        return "Segoe UI"
    return "DejaVu Sans"

# Soft light theme. A left accent bar per row carries the state color, which comes
# from the configured mapping (colors.py) so the widget matches the light/bulb.
_IDLE = "#9ca3af"    # grey: idle has no configured color (the light is off)
_BORDER = "#c9ced8"
_BG = "#e9ecf1"      # soft cool grey card (calmer than stark white)
_FG = "#272b31"
_DIM = "#6b7280"
_DIMMER = "#959ba4"
_RULE = "#d6dae1"
_HEADER = "#2b3038"

# When a session needs you, the whole panel takes the blocked (or error) color,
# not just one row, so a blocked session is unmissable across several windows.
# Other states keep the calm grey chrome.
_CALM = {"frame": _BORDER, "header_bg": _BG, "header_fg": _HEADER, "wash": _BG, "alarm": False}


def _mix(rgb, toward, amount: float) -> str:
    """Blend rgb toward another rgb by `amount` (0..1), as a Tk hex string."""
    return colors.rgb_to_hex(
        [round(a + (b - a) * amount) for a, b in zip(rgb, toward)]
    ).lower()


def state_hex(rgb) -> str:
    """Accent color for a resolved row/aggregate color (None = idle grey)."""
    return colors.rgb_to_hex(rgb).lower() if rgb else _IDLE


def text_hex(rgb) -> str:
    """A darker shade of the state color, readable as text on the light card."""
    return _mix(rgb, (0, 0, 0), 0.25) if rgb else _IDLE


def _palette(agg: str, rgb) -> dict:
    if agg in ("blocked", "error") and rgb:
        r, g, b = rgb
        light_bg = 0.299 * r + 0.587 * g + 0.114 * b > 160
        return {"frame": state_hex(rgb), "header_bg": state_hex(rgb),
                "header_fg": _HEADER if light_bg else "#ffffff",
                "wash": _mix(rgb, (255, 255, 255), 0.9), "alarm": True}
    return _CALM


def glyph(state: str) -> str:
    """Filled dot for live states, hollow for idle (used in the header)."""
    return "○" if state == "idle" else "●"


def row_fields(row: dict, now: float):
    """Pure helper: (glyph, project, agent, state, age) display strings."""
    state = row.get("state", "idle")
    age = "—" if state == "working" else _fmt_age(now - row.get("ts", now))
    project = str(row.get("project") or "?")[:18]
    agent = str(row.get("agent") or "?")[:7]
    return glyph(state), project, agent, state, age


def _macos_all_spaces_behavior():
    """The NSWindow collectionBehavior mask that makes the panel appear on every
    Space, or None without pyobjc. CanJoinAllSpaces only -- deliberately NOT
    FullScreenAuxiliary, so the panel follows you across normal Spaces but does
    not overlay a full-screen app (Keynote, video, a full-screen terminal Space),
    which would have no in-app way to dismiss it."""
    try:
        from AppKit import NSWindowCollectionBehaviorCanJoinAllSpaces
        return NSWindowCollectionBehaviorCanJoinAllSpaces
    except Exception:
        return None


class Widget:
    def __init__(self, interval_ms: int = 1000):
        self.interval_ms = interval_ms
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        # The root acts as a 1px border so the panel reads on a light desktop.
        self.root.configure(bg=_BORDER)
        self.card = tk.Frame(self.root, bg=_BG)
        self.card.pack(fill="both", expand=True, padx=1, pady=1)

        fam = _font_family()
        self._f_title = tkfont.Font(family=fam, size=9, weight="bold")
        self._f_proj = tkfont.Font(family=fam, size=9)
        self._f_dim = tkfont.Font(family=fam, size=8)
        self._f_dot = tkfont.Font(family=fam, size=10)

        self.header = tk.Frame(self.card, bg=_BG)
        self.header.pack(fill="x", padx=11, pady=(5, 4))
        self._agg_dot = tk.Label(self.header, text="●", bg=_BG, fg=_DIMMER, font=self._f_dot)
        self._agg_dot.pack(side="left")
        self._title = tk.Label(self.header, text=" vibesignal", bg=_BG, fg=_HEADER,
                               font=self._f_title)
        self._title.pack(side="left")
        self._count = tk.Label(self.header, text="", bg=_BG, fg=_DIMMER, font=self._f_dim)
        self._count.pack(side="right")

        self._rule = tk.Frame(self.card, bg=_RULE, height=1)
        self._rule.pack(fill="x", padx=11)

        self.body = tk.Frame(self.card, bg=_BG)
        self.body.pack(fill="both", expand=True, padx=(8, 12), pady=(5, 8))
        self.body.grid_columnconfigure(0, minsize=4)              # accent bar
        self.body.grid_columnconfigure(1, minsize=118, weight=1)  # project
        self.body.grid_columnconfigure(2, minsize=46)             # agent
        self.body.grid_columnconfigure(3, minsize=54)             # state
        self.body.grid_columnconfigure(4, minsize=40)             # age

        for w in (self.root, self.card, self.header, self._agg_dot, self._title, self._count):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<Button-3>", self._menu)
            if sys.platform == "darwin":
                # Some macOS Tk builds report the right mouse button as Button-2,
                # and Control-click is the historical single-button right-click.
                # Binding both keeps the Quit menu reachable on every Mac setup.
                w.bind("<Button-2>", self._menu)
                w.bind("<Control-Button-1>", self._menu)

        self._cells: list[tk.Widget] = []
        self._drag = (0, 0)
        self._tick()
        self.root.after(60, self._reposition)

    def _menu(self, event):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Quit panel", command=self.root.destroy)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _start_drag(self, event):
        self._drag = (event.x, event.y)

    def _on_drag(self, event):
        x = self.root.winfo_x() + (event.x - self._drag[0])
        y = self.root.winfo_y() + (event.y - self._drag[1])
        self.root.geometry(f"+{x}+{y}")

    def _screen_workarea(self):
        """(left, top, right, bottom) of the desktop work area.

        Per-platform: Windows uses SystemParametersInfoW SPI_GETWORKAREA so the
        taskbar is excluded; macOS uses NSScreen.visibleFrame (via pyobjc, if
        installed) so the menu bar and Dock are excluded, falling back to a
        28/80 px heuristic when pyobjc is absent; other systems fall back to
        the full screen.
        """
        if sys.platform == "darwin":
            try:
                from AppKit import NSScreen  # pyobjc; best-effort
                screen = NSScreen.mainScreen()
                if screen is not None:
                    visible = screen.visibleFrame()
                    full = screen.frame()
                    # NSScreen uses bottom-left origin; Tk uses top-left. Flip Y
                    # against the screen's OWN top edge, not against full.size.height
                    # alone, so the math stays correct when mainScreen() resolves
                    # to a non-primary display (origin.y != 0 in a vertical
                    # multi-display layout).
                    screen_top = full.origin.y + full.size.height
                    left = int(visible.origin.x)
                    right = int(visible.origin.x + visible.size.width)
                    top = int(screen_top - (visible.origin.y + visible.size.height))
                    bottom = int(screen_top - visible.origin.y)
                    return left, top, right, bottom
            except Exception:
                pass
            # Heuristic without pyobjc: subtract the menu bar (~28) and a
            # bottom-Dock-sized strip (~80). Assumes a bottom Dock; a left or
            # right Dock will overlap the widget at the default x=14 origin.
            # Install pyobjc-framework-Cocoa for true Dock-aware placement.
            sh = self.root.winfo_screenheight()
            sw = self.root.winfo_screenwidth()
            return 0, 28, sw, sh - 80
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            # SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _reposition(self):
        self.root.update_idletasks()
        left, _top, _right, bottom = self._screen_workarea()
        height = self.root.winfo_height()
        self.root.geometry(f"+{left + 14}+{bottom - height - 12}")

    def _keep_on_top(self):
        """Re-assert always-on-top each tick, on macOS and Windows.

        A single startup -topmost does not hold for a borderless (overrideredirect)
        window on either OS. macOS Aqua drops it below other apps when they
        activate. On Windows another window that later becomes topmost sits above
        ours in the same topmost band, and re-setting Tk's -topmost to the value it
        already has is a no-op (Tk caches the attribute), so the window stays buried
        until something re-inserts it at the top of the band. Both are fixed by
        re-asserting every tick. Linux window managers hold the startup -topmost, so
        nothing is needed there and a per-tick re-raise would be needless z-order
        churn.

        macOS pins the native NSWindow floating level via pyobjc when present, else
        the stdlib -topmost + lift() re-raise. Windows re-pins HWND_TOPMOST via
        SetWindowPos, else the same stdlib fallback.
        """
        if sys.platform == "darwin":
            if self._macos_float_level():
                return
            self._topmost_reassert_tk()
        elif sys.platform == "win32":
            if self._windows_pin_topmost():
                return
            self._topmost_reassert_tk()

    def _topmost_reassert_tk(self):
        """Stdlib always-on-top re-assert: set -topmost and lift above siblings.

        The shared fallback for macOS without pyobjc and for Windows if the native
        SetWindowPos pin is somehow unavailable. On Windows this is weaker than the
        pin (lift() restacks with HWND_TOP, not HWND_TOPMOST), so it is a last
        resort there, not the primary path.
        """
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        except tk.TclError:
            pass

    def _windows_pin_topmost(self) -> bool:
        """Re-pin the window to the top of the Windows topmost band via SetWindowPos,
        without stealing focus. Returns True on success, False to let the caller fall
        back to the Tk re-assert -- and False on any non-Windows box (ctypes.windll
        is absent there), so the method is safe to call on every platform.

        Why raw SetWindowPos and not Tk's -topmost: once another window becomes
        topmost after us it sits above ours, and re-setting -topmost to True (its
        current value) is a Tk no-op, so it never restacks. SetWindowPos with
        HWND_TOPMOST re-inserts us at the top of the band unconditionally.
        SWP_NOACTIVATE keeps input focus where it is, so the panel never grabs your
        keystrokes. GetAncestor(GA_ROOT) resolves the real top-level HWND in case Tk
        wrapped the content window, and argtypes are pinned so a 64-bit HWND is not
        truncated to 32 bits.
        """
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            GA_ROOT = 2
            hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT) or self.root.winfo_id()
            HWND_TOPMOST = wintypes.HWND(-1)
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            ok = user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
            )
            return bool(ok)
        except Exception:
            return False

    def _macos_float_level(self) -> bool:
        """Pin the native NSWindow to a floating level so the panel stays above
        normal windows and follows you across Spaces. Returns True if pyobjc was
        available and the level was set, False otherwise (the caller then uses the
        stdlib -topmost + lift() fallback). Install the ``macos`` extra for this
        path."""
        try:
            from AppKit import NSApplication, NSFloatingWindowLevel
        except Exception:
            return False
        behavior = _macos_all_spaces_behavior()
        try:
            for win in NSApplication.sharedApplication().windows():
                win.setLevel_(NSFloatingWindowLevel)
                if behavior is not None:
                    win.setCollectionBehavior_(behavior)
            return True
        except Exception:
            return False

    def _tick(self):
        rows = resolve.resolve_per_session()
        agg, agg_color = resolve.resolve_color()
        now = time.time()

        pal = _palette(agg, agg_color)
        wash, hbg = pal["wash"], pal["header_bg"]

        # Whole-panel alarm: the frame and header take the blocked/error color and the
        # body is tinted, so a blocked session is visible without reading any row.
        self.root.configure(bg=pal["frame"])
        self.card.configure(bg=wash)
        self.card.pack_configure(padx=(3 if pal["alarm"] else 1),
                                 pady=(3 if pal["alarm"] else 1))
        self.body.configure(bg=wash)
        self.header.configure(bg=hbg)
        self._rule.configure(bg=(hbg if pal["alarm"] else _RULE))
        self._title.configure(bg=hbg, fg=pal["header_fg"])
        self._agg_dot.configure(bg=hbg, fg=(pal["header_fg"] if pal["alarm"] else state_hex(agg_color)))
        self._count.configure(text=(f"{len(rows)}" if rows else ""), bg=hbg,
                              fg=(pal["header_fg"] if pal["alarm"] else _DIMMER))

        for w in self._cells:
            w.destroy()
        self._cells = []

        if not rows:
            lbl = tk.Label(self.body, text="no active sessions", bg=wash, fg=_DIM,
                           font=self._f_dim, anchor="w")
            lbl.grid(row=0, column=1, columnspan=4, sticky="w", pady=2)
            self._cells.append(lbl)
        else:
            for i, r in enumerate(rows):
                _g, project, agent, state, age = row_fields(r, now)
                bar = tk.Frame(self.body, bg=state_hex(r.get("color")))
                bar.grid(row=i, column=0, sticky="nsew", padx=(0, 9), pady=2)
                cells = [
                    bar,
                    tk.Label(self.body, text=project, bg=wash, fg=_FG, font=self._f_proj, anchor="w"),
                    tk.Label(self.body, text=agent, bg=wash, fg=_DIM, font=self._f_dim, anchor="w"),
                    tk.Label(self.body, text=state, bg=wash, fg=text_hex(r.get("color")), font=self._f_dim, anchor="w"),
                    tk.Label(self.body, text=age, bg=wash, fg=_DIMMER, font=self._f_dim, anchor="e"),
                ]
                cells[1].grid(row=i, column=1, sticky="w", pady=1)
                cells[2].grid(row=i, column=2, sticky="w", padx=(8, 0), pady=1)
                cells[3].grid(row=i, column=3, sticky="w", padx=(8, 0), pady=1)
                cells[4].grid(row=i, column=4, sticky="e", padx=(8, 0), pady=1)
                self._cells.extend(cells)

        self._keep_on_top()
        self.root.after(self.interval_ms, self._tick)

    def run(self):
        self.root.mainloop()


def main(interval_ms: int = 1000) -> int:
    Widget(interval_ms=interval_ms).run()
    return 0


if __name__ == "__main__":
    main()
