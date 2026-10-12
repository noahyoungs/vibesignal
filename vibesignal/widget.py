"""Always-on-top floating panel (Tkinter, standard library) over the store.

A small borderless window that stays on top and shows one row per active
session, blocked first, refreshed about once a second. It is the desktop-GUI
sibling of panel.py (the terminal table) and the future physical light: all
three read the same store through resolve_per_session(), so this adds a
renderer without touching the hook layer.

Run with:
  Windows:        pythonw -m vibesignal widget    (no console window)
  macOS / Linux:  vibesignal widget &        (background the GUI)

Drag by the header or the status screen; right-click to quit. On macOS, Control-click works too
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


def _mono_family() -> str:
    """Per-platform monospace family for the small status screen."""
    if sys.platform == "darwin":
        return "Menlo"
    if sys.platform == "win32":
        return "Consolas"
    return "DejaVu Sans Mono"

# Calm night theme, after calm-chords' deep-night mood (src/moods.ts): a body-
# colored card with a small, rounded inset screen showing the aggregate state,
# and a left accent bar per row. Every state color comes from the configured
# mapping (colors.py), so the widget matches the light/bulb.
_BORDER = "#3a3f6e"  # night body, light
_BG = "#23264a"      # night body, dark: the card
_SCREEN = "#101a3a"  # night sky, deepest: the inset status screen
_SCREEN_EDGE = "#2c2560"  # night sky, violet
_RULE = "#2c2560"
_FG = "#dfe3ff"      # night ink
_HEADER = "#dfe3ff"
_DIM = "#a7aac9"     # night ink 30% toward the card
_DIMMER = "#8184a4"  # night ink 50% toward the card
_IDLE = "#7675a4"    # night screen #c8c4ff 50% toward the card: idle has no
                     # configured color (the light is off)
_INK = "#3b2f4a"     # dawn ink: dark text on a light (pastel) alarm header

# When a session needs you, the whole panel takes the blocked (or error) color,
# not just one row, so a blocked session is unmissable across several windows.
# Other states keep the calm night chrome.
_CALM = {"frame": _BORDER, "header_bg": _BG, "header_fg": _HEADER, "wash": _BG,
         "screen": _SCREEN, "alarm": False}

# What the status screen says for each aggregate state: (title, hint).
_SCREEN_TEXT = {
    "blocked": ("needs you", "waiting on you"),
    "error": ("error", "a session failed"),
    "done": ("done", "your move"),
    "working": ("working", "agents busy"),
    "idle": ("all quiet", "nothing live"),
}


def _mix(rgb, toward, amount: float) -> str:
    """Blend rgb toward another rgb by `amount` (0..1), as a Tk hex string."""
    return colors.rgb_to_hex(
        [round(a + (b - a) * amount) for a, b in zip(rgb, toward)]
    ).lower()


def state_hex(rgb) -> str:
    """Accent color for a resolved row/aggregate color (None = idle grey)."""
    return colors.rgb_to_hex(rgb).lower() if rgb else _IDLE


def text_hex(rgb) -> str:
    """A lighter shade of the state color, readable as text on the night card."""
    return _mix(rgb, (255, 255, 255), 0.2) if rgb else _IDLE


def screen_text(agg: str) -> tuple[str, str]:
    """Pure helper: (title, hint) for the status screen."""
    return _SCREEN_TEXT.get(agg, _SCREEN_TEXT["idle"])


def _luminance(rgb) -> float:
    """WCAG relative luminance of an sRGB color (0 = black, 1 = white)."""
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _palette(agg: str, rgb) -> dict:
    if agg in ("blocked", "error") and rgb:
        # Header text is white or ink, whichever contrasts more with the color,
        # so pastel and vivid custom colors both stay legible.
        bg = _luminance(rgb)
        on_white = 1.05 / (bg + 0.05)
        on_ink = (bg + 0.05) / (_luminance(colors.hex_to_rgb(_INK)) + 0.05)
        return {"frame": state_hex(rgb), "header_bg": state_hex(rgb),
                "header_fg": _INK if on_ink > on_white else "#ffffff",
                "wash": _mix(rgb, colors.hex_to_rgb(_BG), 0.84),
                "screen": _mix(rgb, colors.hex_to_rgb(_SCREEN), 0.9), "alarm": True}
    return _CALM


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """A rounded rectangle on a Tk canvas (a smoothed polygon)."""
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
              x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, **kw)


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
        # The root acts as a 1px border so the panel reads on any desktop.
        self.root.configure(bg=_BORDER)
        self.card = tk.Frame(self.root, bg=_BG)
        self.card.pack(fill="both", expand=True, padx=1, pady=1)

        fam = _font_family()
        self._f_title = tkfont.Font(family=fam, size=9, weight="bold")
        self._f_proj = tkfont.Font(family=fam, size=9)
        self._f_dim = tkfont.Font(family=fam, size=8)
        self._f_dot = tkfont.Font(family=fam, size=10)
        mono = _mono_family()
        self._f_screen = tkfont.Font(family=mono, size=12, weight="bold")
        self._f_hint = tkfont.Font(family=mono, size=8)

        self.header = tk.Frame(self.card, bg=_BG)
        self.header.pack(fill="x", padx=11, pady=(6, 4))
        self._agg_dot = tk.Label(self.header, text="●", bg=_BG, fg=_DIMMER, font=self._f_dot)
        self._agg_dot.pack(side="left")
        self._title = tk.Label(self.header, text=" vibesignal", bg=_BG, fg=_HEADER,
                               font=self._f_title)
        self._title.pack(side="left")
        self._count = tk.Label(self.header, text="", bg=_BG, fg=_DIMMER, font=self._f_dim)
        self._count.pack(side="right")

        # The small status screen: a rounded inset display with a soft dot and
        # the aggregate state, redrawn each tick and on resize.
        self.screen = tk.Canvas(self.card, width=280, height=44, bg=_BG,
                                highlightthickness=0, bd=0)
        self.screen.pack(fill="x", padx=10, pady=(2, 8))
        self.screen.bind("<Configure>", lambda _e: self._draw_screen())
        self._screen_view = ("idle", None, _CALM)

        self._rule = tk.Frame(self.card, bg=_RULE, height=1)
        self._rule.pack(fill="x", padx=11)

        self.body = tk.Frame(self.card, bg=_BG)
        self.body.pack(fill="both", expand=True, padx=(8, 12), pady=(6, 9))
        self.body.grid_columnconfigure(0, minsize=4)              # accent bar
        self.body.grid_columnconfigure(1, minsize=118, weight=1)  # project
        self.body.grid_columnconfigure(2, minsize=46)             # agent
        self.body.grid_columnconfigure(3, minsize=54)             # state
        self.body.grid_columnconfigure(4, minsize=40)             # age

        for w in (self.root, self.card, self.header, self._agg_dot, self._title, self._count,
                  self.screen):
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

    def _draw_screen(self):
        agg, rgb, pal = self._screen_view
        c = self.screen
        c.delete("all")
        c.configure(bg=pal["wash"])
        width = c.winfo_width() if c.winfo_width() > 1 else int(c.cget("width"))
        height = int(c.cget("height"))
        _round_rect(c, 1, 1, width - 2, height - 2, 10, fill=pal["screen"], outline=_SCREEN_EDGE)
        mid = height // 2
        if rgb:  # a soft halo behind the dot, like a lit indicator
            halo = _mix(rgb, colors.hex_to_rgb(pal["screen"]), 0.7)
            c.create_oval(13, mid - 9, 31, mid + 9, fill=halo, outline="")
        c.create_oval(17, mid - 5, 27, mid + 5, fill=state_hex(rgb), outline="")
        title, hint = screen_text(agg)
        c.create_text(41, mid, text=title, anchor="w", font=self._f_screen,
                      fill=text_hex(rgb) if rgb else _DIM)
        c.create_text(width - 14, mid, text=hint, anchor="e", font=self._f_hint, fill=_DIMMER)

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
        self._screen_view = (agg, agg_color, pal)
        self._draw_screen()

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
