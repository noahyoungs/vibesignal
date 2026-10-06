"""Command-line entry point, invoked by agent hooks.

Usage:
  vibesignal event --agent claude --state working
  vibesignal status
  vibesignal clear [--agent A --session S]
  vibesignal off

The `event` command reads the session id from the hook's stdin JSON when
`--session` is not given, and always exits 0 so a hook can never block the agent.
The stdin read is bounded by a short timeout, so an open but dataless pipe cannot
hang the hook; the record -> resolve -> set-light -> cache critical section is held
under a cross-process lock so concurrent hooks cannot race the device.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading

from . import light, lock, store
from .resolve import State, resolve_color, resolve_per_session


def _default_stdin_timeout() -> float:
    # Hook stdin must be available within this window, or the event falls back to
    # the default session. Override for slower wrapper layers.
    raw = os.environ.get("VIBECODING_STDIN_TIMEOUT", "0.5")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.5


STDIN_TIMEOUT_SECONDS = _default_stdin_timeout()


def _read_hook_stdin(timeout: float = STDIN_TIMEOUT_SECONDS) -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}

    result: "queue.Queue[str]" = queue.Queue(maxsize=1)

    def read_stdin() -> None:
        try:
            raw = sys.stdin.read()
        except Exception:
            raw = ""
        try:
            result.put_nowait(raw)
        except queue.Full:
            pass

    # Daemon thread: if stdin never reaches EOF, the read is abandoned at exit
    # instead of hanging the hook.
    threading.Thread(target=read_stdin, daemon=True).start()
    try:
        raw = result.get(timeout=timeout)
    except queue.Empty:
        return {}

    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _apply_light() -> tuple[str, list | None]:
    state, color = resolve_color()
    if color != store.get_last_color():
        # Cache the color only after the device accepts it, so a no-device run
        # does not poison the cache and suppress the first real write later.
        if light.set_color(color):
            store.set_last_color(color)
    return state, color


def cmd_event(args) -> int:
    try:
        hook = _read_hook_stdin()
        session = args.session or hook.get("session_id") or "default"
        cwd = str(hook.get("cwd") or os.getcwd())
        project = args.project or os.path.basename(cwd.rstrip("/\\")) or None
        with lock.file_lock(store.lock_path()):
            store.record(agent=args.agent, session=session, state=args.state, project=project)
            state, color = _apply_light()
        # --quiet keeps stdout empty for hosts that parse hook stdout as JSON
        # (Codex rejects a plain status line from a PostToolUse hook).
        if not args.quiet:
            print(f"[vibesignal] {args.agent}/{session}: {args.state} -> {state} {color}")
    except Exception as exc:  # a VibeSignal bug must never break the agent
        print(f"[vibesignal] non-fatal: {exc}", file=sys.stderr)
    return 0


def cmd_status(args) -> int:
    # Use the resolver, not raw store.load_active, so `status` agrees with the
    # light and the panel: same per-state lifetime, blocked sorted first. Reading
    # the store directly would hide a long-blocked session the light still shows.
    rows = resolve_per_session()
    state, color = resolve_color()
    print(f"aggregate: {state}  color: {color}  (last applied: {store.get_last_color()})")
    for r in rows:
        print(f"  {r['agent']}/{r['session']}: {r['state']} project={r['project']}")
    if not rows:
        print("  (no active sessions)")
    return 0


def cmd_clear(args) -> int:
    with lock.file_lock(store.lock_path()):
        store.clear(agent=args.agent, session=args.session)
        _apply_light()
    print("[vibesignal] cleared")
    return 0


def cmd_off(args) -> int:
    with lock.file_lock(store.lock_path()):
        store.clear()
        # Force the device off, but cache None only if the write was accepted,
        # mirroring _apply_light so a failed write is not recorded as applied.
        if light.set_color(None):
            store.set_last_color(None)
    print("[vibesignal] off")
    return 0


def cmd_end(args) -> int:
    # SessionEnd hook: clear just the ending session (id from hook stdin) so a
    # closed session leaves the panel at once instead of waiting out the TTL.
    try:
        hook = _read_hook_stdin()
        session = args.session or hook.get("session_id")
        if not session:
            # Unlike `event`, `end` must not fall back to "default": a SessionEnd
            # with no id would otherwise clear an unrelated default-bucket session.
            if not args.quiet:
                print(f"[vibesignal] end ignored for {args.agent}: no session id")
            return 0
        with lock.file_lock(store.lock_path()):
            store.clear(agent=args.agent, session=session)
            _apply_light()
        if not args.quiet:
            print(f"[vibesignal] ended {args.agent}/{session}")
    except Exception as exc:  # a VibeSignal bug must never break the agent
        print(f"[vibesignal] non-fatal: {exc}", file=sys.stderr)
    return 0


def cmd_watch(args) -> int:
    from . import panel
    return panel.watch(interval=args.interval, once=args.once)


def cmd_widget(args) -> int:
    from . import widget
    return widget.main(interval_ms=int(args.interval * 1000))


def cmd_install_launcher(args) -> int:
    from . import installer
    dest = installer.install_launcher()
    print(f"[vibesignal] installed launcher: {dest}")
    if sys.platform == "win32":
        print("Launch from the Start menu (type 'VibeSignal') or the Desktop shortcut.")
    else:
        print("Open via Spotlight ('VibeSignal'), or drag the .app to the Dock.")
    return 0


def cmd_uninstall_launcher(args) -> int:
    from . import installer
    removed = installer.uninstall_launcher()
    print(f"[vibesignal] {'removed launcher' if removed else 'no launcher found'}")
    return 0


def cmd_install_autostart(args) -> int:
    from . import installer
    launch_now = not getattr(args, "no_launch", False)
    dest = installer.install_autostart(launch_now=launch_now)
    if sys.platform == "win32":
        print(f"[vibesignal] installed autostart shortcut: {dest}")
        print("The widget starts now and at every future login." if launch_now
              else "The widget will start at the next login (run 'vibesignal widget' to start it now).")
    else:
        print(f"[vibesignal] installed autostart LaunchAgent: {dest}")
        print("Widget starts now (RunAtLoad=true) and at every future login." if launch_now
              else "Widget will start at the next login (run 'vibesignal widget &' to start it now).")
    if launch_now:
        print("Close any manually opened widget first to avoid duplicate processes.")
    print("Re-run after switching env to re-pin the path.")
    return 0


def cmd_uninstall_autostart(args) -> int:
    from . import installer
    removed = installer.uninstall_autostart()
    print(f"[vibesignal] {'removed autostart' if removed else 'no autostart found'}")
    return 0


def cmd_install_hooks(args) -> int:
    from . import installer
    path = installer.install_hooks(agent=args.agent)
    print(f"[vibesignal] wired {args.agent} hooks into {path}")
    if args.agent == "codex":
        print("Trust the new hooks once via /hooks in a Codex session, then restart it.")
    elif args.agent == "opencode":
        print("Restart OpenCode so it loads the global VibeSignal plugin.")
    else:
        print("Takes effect on the next Claude Code session (or a settings reload).")
    print("Re-run after switching env to re-pin the path.")
    return 0


def cmd_uninstall_hooks(args) -> int:
    from . import installer
    removed = installer.uninstall_hooks(agent=args.agent)
    print(f"[vibesignal] {'removed' if removed else 'no'} {args.agent} hooks")
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vibesignal")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_event = sub.add_parser("event", help="record a session state and update the light")
    p_event.add_argument("--agent", required=True, help="agent name, e.g. claude or codex")
    p_event.add_argument(
        "--state", required=True,
        choices=[State.WORKING, State.BLOCKED, State.DONE, State.ERROR, State.IDLE, State.NEEDS_INPUT],
    )
    p_event.add_argument("--session", default=None, help="session id (else from hook stdin)")
    p_event.add_argument("--project", default=None, help="project tag (else from cwd)")
    p_event.add_argument("--quiet", action="store_true", help="suppress normal stdout for strict hook parsers")
    p_event.set_defaults(func=cmd_event)

    p_status = sub.add_parser("status", help="print active sessions and the resolved color")
    p_status.set_defaults(func=cmd_status)

    p_clear = sub.add_parser("clear", help="clear one or all sessions")
    p_clear.add_argument("--agent", default=None)
    p_clear.add_argument("--session", default=None)
    p_clear.set_defaults(func=cmd_clear)

    p_off = sub.add_parser("off", help="clear all sessions and turn the light off")
    p_off.set_defaults(func=cmd_off)

    p_end = sub.add_parser("end", help="clear one ended session (id from hook stdin)")
    p_end.add_argument("--agent", required=True, help="agent name, e.g. claude")
    p_end.add_argument("--session", default=None, help="session id (else from hook stdin)")
    p_end.add_argument("--quiet", action="store_true", help="suppress normal stdout for strict hook parsers")
    p_end.set_defaults(func=cmd_end)

    p_watch = sub.add_parser("watch", help="live multi-session panel (foreground viewer)")
    p_watch.add_argument("--interval", type=float, default=1.0, help="refresh seconds")
    p_watch.add_argument("--once", action="store_true", help="render once and exit")
    p_watch.set_defaults(func=cmd_watch)

    p_widget = sub.add_parser("widget", help="always-on-top floating GUI panel")
    p_widget.add_argument("--interval", type=float, default=1.0, help="refresh seconds")
    p_widget.set_defaults(func=cmd_widget)

    p_install_launcher = sub.add_parser(
        "install-launcher",
        help="install a one-click launcher (macOS .app, or Windows Start menu + Desktop shortcut)",
    )
    p_install_launcher.set_defaults(func=cmd_install_launcher)

    p_uninstall_launcher = sub.add_parser(
        "uninstall-launcher",
        help="remove the one-click launcher",
    )
    p_uninstall_launcher.set_defaults(func=cmd_uninstall_launcher)

    p_install_autostart = sub.add_parser(
        "install-autostart",
        help="install login autostart (macOS LaunchAgent, or Windows Startup shortcut) and start the widget now",
    )
    p_install_autostart.add_argument(
        "--no-launch", action="store_true",
        help="install the autostart entry but do not start the widget now (it starts at the next login)",
    )
    p_install_autostart.set_defaults(func=cmd_install_autostart)

    p_uninstall_autostart = sub.add_parser(
        "uninstall-autostart",
        help="remove login autostart",
    )
    p_uninstall_autostart.set_defaults(func=cmd_uninstall_autostart)

    p_install_hooks = sub.add_parser(
        "install-hooks",
        help="wire agent hooks into the settings file, absolute path pinned (all platforms)",
    )
    p_install_hooks.add_argument(
        "--agent", default="claude", choices=["claude", "codex", "opencode"],
        help="which agent's settings file to wire (default: claude)",
    )
    p_install_hooks.set_defaults(func=cmd_install_hooks)

    p_uninstall_hooks = sub.add_parser(
        "uninstall-hooks",
        help="remove vibesignal hooks from an agent settings file",
    )
    p_uninstall_hooks.add_argument(
        "--agent", default="claude", choices=["claude", "codex", "opencode"],
        help="which agent's settings file to clean (default: claude)",
    )
    p_uninstall_hooks.set_defaults(func=cmd_uninstall_hooks)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
