"""Command-line entry point, invoked by agent hooks.

Usage:
  vibesignal event --agent claude --state working
  vibesignal status
  vibesignal clear [--agent A --session S]
  vibesignal off
  vibesignal config [--color STATE HEX | --theme NAME | --export-colors PATH ...]
  vibesignal matter-config [--brightness PCT ...]

The `event` command reads the session id from the hook's stdin JSON when
`--session` is not given, and always exits 0 so a hook can never block the agent.
The stdin read is bounded by a short timeout, so an open but dataless pipe cannot
hang the hook; the record -> resolve -> set-light -> cache critical section is held
under a cross-process lock so concurrent hooks cannot race the device.
"""

from __future__ import annotations

import argparse
import getpass
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
            # A source that fires several events at once (OpenCode spawns one
            # process per event) passes --ts with the time each event happened.
            # Processes can finish in any order, so an event older than the
            # session's current record is stale and must not overwrite it.
            previous = store.read_session(args.agent, session) if args.ts else None
            prev_ts = (previous or {}).get("ts")
            if not (isinstance(prev_ts, (int, float)) and prev_ts > args.ts):
                transcript = hook.get("transcript_path")
                store.record(agent=args.agent, session=session, state=args.state,
                             project=project, now=args.ts,
                             transcript=transcript if isinstance(transcript, str) else None)
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
    matter_status = store.get_matter_status()
    print(f"matter: color={matter_status.get('color')} error={matter_status.get('error') or 'none'}")
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


def cmd_matter_sync(args) -> int:
    from . import matter
    return matter.run_sync(interval=args.interval, once=args.once)


def _percent(value: str) -> int:
    """argparse type: an integer brightness percentage from 1 to 100."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number from 1 to 100, got {value!r}")
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError(f"must be from 1 to 100, got {number}")
    return number


def _non_negative(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}")
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {number}")
    return number


def _describe_matter(config) -> str:
    return (
        f"node={config.node_id} endpoint={config.endpoint_id} "
        f"brightness={config.brightness_pct}% transition={config.transition_time / 10:g}s "
        f"server={config.url}"
    )


def cmd_matter_config(args) -> int:
    from dataclasses import replace

    from .matter import load_config, save_config
    if args.node is not None and args.node < 1:
        raise SystemExit("vibesignal: --node must be a commissioned node id (1 or more)")
    current = load_config()
    updates = {
        "url": args.url,
        "node_id": args.node,
        "endpoint_id": args.endpoint,
        "transition_time": args.transition,
        "brightness_pct": args.brightness,
    }
    config = replace(current, **{k: v for k, v in updates.items() if v is not None})
    if args.clear_node:
        config = replace(config, node_id=None)
    # Saving also rewrites a legacy max/steady/pulse file in the new format.
    # The running `matter-sync` worker re-reads this file on its next tick.
    save_config(config)
    if config.node_id is None:
        store.set_matter_error(None)
    print(f"[vibesignal] Matter {_describe_matter(config)}")
    return 0


def _apply_light_now() -> None:
    """Re-render the USB light after a color change (no-op without a device)."""
    try:
        with lock.file_lock(store.lock_path()):
            _apply_light()
    except Exception as exc:
        print(f"[vibesignal] colors saved; USB light not updated: {exc}", file=sys.stderr)


def _print_colors(colors_map: dict, warning: str | None = None) -> None:
    from . import colors
    theme = colors.theme_name(colors_map) or "custom"
    print(f"colors (theme: {theme}; {colors.colors_path()})")
    for state in colors.ACTIVE_STATES:
        print(f"  {state:<8} {colors_map[state]}")
    print(f"  {'idle':<8} off")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)


def cmd_config(args) -> int:
    from . import colors
    from .matter import config_path, load_config
    try:
        if args.list_themes:
            current = colors.theme_name(colors.load_colors())
            for name, theme in colors.THEMES.items():
                mark = "*" if name == current else " "
                swatches = "  ".join(f"{s}={theme[s]}" for s in colors.ACTIVE_STATES)
                print(f"{mark} {name:<8} {swatches}")
            return 0
        if args.export_colors:
            exported = colors.export_colors(args.export_colors)
            print(f"[vibesignal] exported {colors.theme_name(exported) or 'custom'} colors "
                  f"to {args.export_colors}")
            return 0
        if args.color:
            updated = colors.set_colors(dict(args.color))
        elif args.theme:
            updated = colors.apply_theme(args.theme)
        elif args.reset_colors:
            updated = colors.reset_colors()
        elif args.import_colors:
            updated = colors.import_colors(args.import_colors)
        else:  # --show, or no option at all
            effective, warning = colors.read_saved()
            _print_colors(effective, warning)
            matter = load_config()
            print(f"matter ({config_path()})")
            print(f"  {_describe_matter(matter)}")
            return 0
    except colors.ColorConfigError as exc:
        raise SystemExit(f"vibesignal: {exc}")
    _apply_light_now()
    _print_colors(updated)
    print("[vibesignal] saved; the Matter worker and widget pick this up within a second")
    return 0


def cmd_install_matter_autostart(args) -> int:
    from . import installer
    paths = installer.install_matter_autostart(interface=args.interface)
    print("[vibesignal] installed Matter services:")
    for path in paths:
        print(f"  {path}")
    print("Matter dashboard: http://127.0.0.1:5580")
    return 0


def cmd_uninstall_matter_autostart(args) -> int:
    from . import installer
    removed = installer.uninstall_matter_autostart()
    print(f"[vibesignal] {'removed Matter services' if removed else 'no Matter services found'}")
    return 0


def cmd_matter_nodes(args) -> int:
    from .matter import list_nodes, load_config
    nodes = list_nodes(load_config())
    if not nodes:
        print("(no commissioned Matter nodes)")
        return 0
    for node in nodes:
        print(json.dumps(node, indent=2, default=str))
    return 0


def cmd_matter_commission(args) -> int:
    from dataclasses import replace

    from .matter import commission, load_config, save_config
    current = load_config()
    code = args.code or getpass.getpass("Matter sharing/pairing code: ")
    print("[vibesignal] commissioning Matter device; this may take up to 2 minutes...", flush=True)
    node = commission(current, code, network_only=not args.ble)
    save_config(replace(current, node_id=node, endpoint_id=args.endpoint))
    print(f"[vibesignal] commissioned and selected Matter node {node}, endpoint {args.endpoint}")
    return 0


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
    p_event.add_argument(
        "--ts", type=float, default=None,
        help="event time (epoch seconds); an event older than the session's record is ignored",
    )
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

    p_matter_sync = sub.add_parser("matter-sync", help="mirror aggregate state to a Matter bulb")
    p_matter_sync.add_argument("--interval", type=float, default=0.5)
    p_matter_sync.add_argument("--once", action="store_true")
    p_matter_sync.set_defaults(func=cmd_matter_sync)

    p_matter_config = sub.add_parser(
        "matter-config",
        help="configure the Matter bulb target and brightness (unset flags keep their value)",
    )
    p_matter_config.add_argument("--url", default=None, help="Matter Server WebSocket URL")
    p_matter_config.add_argument("--node", type=int, default=None, help="commissioned node id")
    p_matter_config.add_argument("--clear-node", action="store_true", help="stop driving any bulb")
    p_matter_config.add_argument("--endpoint", type=_non_negative, default=None, help="light endpoint (usually 1)")
    p_matter_config.add_argument(
        "--transition", type=_non_negative, default=None,
        help="color/level transition in tenths of a second (default 3)",
    )
    p_matter_config.add_argument(
        "--brightness", type=_percent, default=None, metavar="PERCENT",
        help="constant bulb brightness for every active state, 1-100 percent",
    )
    p_matter_config.set_defaults(func=cmd_matter_config)

    p_config = sub.add_parser(
        "config",
        help="show or change state colors and themes (no option: --show)",
        description=(
            "State colors apply to the USB light, the widget, and the Matter bulb. "
            "idle is always off. Matter brightness is set separately with "
            "`vibesignal matter-config --brightness`."
        ),
    )
    config_action = p_config.add_mutually_exclusive_group()
    config_action.add_argument("--show", action="store_true", help="print effective colors and Matter settings")
    config_action.add_argument(
        "--color", nargs=2, action="append", metavar=("STATE", "HEX"),
        help="set one state color, e.g. --color working '#5CCCB9' (repeatable)",
    )
    config_action.add_argument("--theme", metavar="NAME", help="apply a built-in theme to all four states")
    config_action.add_argument("--list-themes", action="store_true", help="list built-in themes")
    config_action.add_argument("--reset-colors", action="store_true", help="restore the default colors")
    config_action.add_argument("--export-colors", metavar="PATH", help="write the effective colors to a JSON file")
    config_action.add_argument("--import-colors", metavar="PATH", help="replace all colors from a JSON file")
    p_config.set_defaults(func=cmd_config)

    p_matter_nodes = sub.add_parser("matter-nodes", help="list commissioned Matter nodes")
    p_matter_nodes.set_defaults(func=cmd_matter_nodes)

    p_matter_commission = sub.add_parser("matter-commission", help="commission and select the bulb")
    p_matter_commission.add_argument("--code", default=None, help="pairing code (omit for hidden prompt)")
    p_matter_commission.add_argument("--endpoint", type=int, default=1)
    p_matter_commission.add_argument("--ble", action="store_true", help="allow BLE commissioning")
    p_matter_commission.set_defaults(func=cmd_matter_commission)

    p_install_matter = sub.add_parser("install-matter-autostart", help="start Matter Server and bulb sync at login")
    p_install_matter.add_argument("--interface", default="en0", help="primary network interface (default: en0)")
    p_install_matter.set_defaults(func=cmd_install_matter_autostart)

    p_uninstall_matter = sub.add_parser("uninstall-matter-autostart", help="remove Matter Server and sync services")
    p_uninstall_matter.set_defaults(func=cmd_uninstall_matter_autostart)

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
