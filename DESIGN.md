# VibeSignal Design

Status: v1 single-beacon (commit `dbcb728`) and v2 multi-session panel (commit `cca7a37`) shipped upstream. This document covers v1 through v3 (the always-on-top floating widget, the done-fade, and the session-end clear) plus this fork's additions: the Matter bulb worker, the OpenCode plugin, and configurable colors.
Date: 2026-05-29 (upstream design); fork additions 2026-10

## Goal

Show, at a glance, which of several concurrent coding-agent sessions needs the user's attention. The user runs 4-5 Claude Code sessions (plus Codex) in the terminal at once; a single aggregate beacon ("an agent needs you") forces hunting across terminals. v2 adds a per-session view that names which session is blocked.

## Non-Goals

- Auto-detected error state for Claude Code and Codex: neither has a clean "task failed" hook, so automatic error coloring is out for them (a noisy false signal is worse than none) and `error` stays a manual, explicit state. The OpenCode plugin maps its explicit `session.error` event to `error`.
- A background daemon for the USB light: the USB light is driven synchronously by hooks, and solid colors persist in the hardware after the hook exits. The panel and the widget are viewers that only read the store and never drive the USB light.
- Network I/O in hooks: the Matter bulb is the one exception to "no service". A network round-trip per hook would slow every agent event, and only a running process can turn the bulb off when a session's lifetime expires with no further event. So a single persistent worker (`matter-sync`) owns the bulb; hooks still only write the store. See Matter Worker below.
- The physical multi-LED strip: out of this round, but the architecture leaves a clean seam for it (see Future Work).

## Architecture: One Store, Many Renderers

The design separates state from display. Hooks write per-session state to a file store; each display reads that store. Adding the panel does not change the hook layer or the light.

```
hooks (claude + codex + opencode plugin)  -->  vibesignal event --agent X --state Y
        |
        v
   store  ~/.vibesignal/sessions/*.json     # source of truth: per-session, agent-tagged
        |
        |- resolve_color()        -> one color  -->  single light (light.py)       [v1 path, recolored]
        |                                       \-->  matter-sync worker -> bulb   [fork, persistent]
        |- resolve_per_session()  -> rows                                          [v2, shared primitive]
                |
                |-->  panel:  `vibesignal watch`   (TUI table)              [v2]
                |-->  widget: `vibesignal widget`  (always-on-top GUI)      [v3]
                \-->  strip:  rows + session->cell map -> multi-LED                [future, reuses the primitive]
```

`resolve_per_session()` is the shared building block. The terminal panel renders it as table rows, the v3 floating widget renders it as an always-on-top GUI, and a future LED strip maps it to cells. Each renderer is additive over the same primitive, so no work is discarded. The single light keeps calling the same `resolve_color()` entry point; only its color map and priority gained the blue-split states.

## State Taxonomy (Blue-Split)

v1 mapped both `Notification` and `Stop` to one `needs_input` state. v2 split them so the user can tell "act now" from "your move", and v3 refined which hook sets which state:

| State | Color | Set by (Claude Code hook) | Meaning |
|-------|-------|---------------------------|---------|
| `working` | green | UserPromptSubmit, PostToolUse | running; leave alone |
| `blocked` | red | Notification (`permission_prompt`) | needs you now (permission / question) |
| `done` | blue | Stop, StopFailure, Notification (`idle_prompt`) | finished the turn; your move |
| `error` | violet | (manual; OpenCode `session.error`) | failure |
| `idle` | off (grey in the widget) | TTL timeout | nothing pending |

The colors above are the defaults (`working #00C83C`, `blocked #DC2626`, `done #005AFF`, `error #9333EA`); they are user-configurable, see Configurable Colors. Earlier upstream drafts used amber for `blocked` and red for `error`.

The `Notification` hook fires for more than one situation, so v3 splits it by matcher: a `permission_prompt` is `blocked` (act now), while an `idle_prompt` (the turn ended and is waiting on you) is `done`. A flat `Notification` to `blocked` produced false "blocked" on idle prompts, which is why the matcher split matters. `StopFailure` (a turn that ends on an API error) also maps to `done`: the turn is over, so the session is no longer working.

Single-light aggregate priority (highest wins across active sessions): `blocked > error > done > working > idle`. If any session is blocked, the one light shows the blocked color, so the most urgent signal is never hidden.

### Per-State Lifetime

Each state has its own lifetime on the panel, because the hooks only refresh a session's timestamp when something happens. `working` refreshes on every tool call, so a working session that goes silent for `WORKING_TTL_SECONDS` (10 min) is treated as dead. `done` is a transient "your move" pulse that fades after `DONE_TTL_SECONDS` (90 s). `error` is a transient alert that fades after `ERROR_TTL_SECONDS` (5 min). `blocked` is the opposite: nothing refreshes it while it waits on you, so a short TTL would make a long-pending prompt vanish exactly when it is most overdue. It persists for `BLOCKED_TTL_SECONDS` (8 h), long enough to span a workday, and clear sooner when you act (the state changes), when `SessionEnd` fires, or on a manual clear. The 8 h backstop only self-cleans a hard-crashed session that left no final event.

The per-state cutoff (`age > ttl`) is applied by `_expired` in both `resolve_color()` and `resolve_per_session()`, so the light and every renderer agree. The resolver loads every file within the longest TTL, then applies the per-state rule; a file is not deleted, it is filtered out of the views, matching the existing "stale files are ignored, not removed" policy.

Backward compatibility: `needs_input` is kept as an accepted alias for `blocked`, so a v1 hook snippet still works.

## Components

- `store.py`: unchanged. Already per-session, agent-tagged, atomic, TTL-bounded, malformed-file tolerant.
- `resolve.py`: `BLOCKED` and `DONE` states and their colors; `needs_input` maps to `blocked`; aggregate priority; `resolve_per_session(ttl)` returns rows `{agent, project, session, state, color, ts}`, sorted blocked-first; renderers compute display age from `ts`. v3 adds the per-state lifetime (`_STATE_TTL_SECONDS`, `_expired`): `working` 10 min, `done` 90 s, `error` 5 min, `blocked` an 8 h backstop, applied by both `resolve_color()` and `resolve_per_session()`. Colors are not defined here; they come from `colors.py`.
- `panel.py`: `render(rows, now) -> str` formats rows into a colored table (testable without a terminal); `watch()` reads the store, calls `render`, reprints about once a second, and exits cleanly on Ctrl-C.
- `widget.py` (v3, new): an always-on-top Tkinter window (standard library only) that renders `resolve_per_session()` as a small floating panel, one row per session, blocked first. A third renderer over the same primitive, so it adds no hook-layer or store change. Borderless, drag by the header, right-click to quit, pinned bottom-left of the work area.
- `__main__.py`: `watch`, `widget`, and `end` subcommands; `event --state` choices include `blocked` and `done` (with `needs_input` as an alias). `end` clears one session by id from hook stdin, and no-ops when no id is present so it cannot wipe the `default` bucket.
- `light.py`: unchanged (still driven by the `resolve_color` color).
- `hooks/claude-settings.snippet.json`: the deployed model. UserPromptSubmit and PostToolUse to `working`; `Notification` split (`permission_prompt` to `blocked`, `idle_prompt` to `done`); `Stop` and `StopFailure` to `done`; `SessionEnd` to `end`.
- `hooks/codex-hooks.md`: the Codex side. The same per-turn mapping via the Codex hooks system (`PermissionRequest` for blocked), with `SessionEnd` to `end` on current Codex releases; an older Codex without that event lets a closed session age out by its per-state lifetime.
- `colors.py` (fork): the configurable state-to-color mapping, built-in themes, and the versioned JSON import/export format.
- `matter.py` (fork): the Matter Server WebSocket client, the Matter config (with legacy brightness migration), and the `matter-sync` worker loop.
- OpenCode plugin (fork, generated by `installer.py`): a dependency-free global plugin that maps OpenCode session events to `vibesignal event` / `end` and ignores child (sub-agent) sessions. One file serves both plugin APIs through its default export: OpenCode 1.x calls `server` (session hooks), OpenCode 2.x (bundled by OpenChamber) calls `setup` and consumes `api.event.subscribe()` as an async iterable. 2.x runs `setup` once per project location while every instance sees the same events, so events are handled once by id across instances; each emit passes `--ts` (the event's creation time) so racing processes cannot regress state, and children are spawned detached because 2.x can exit right after a turn's final event.

## Panel UX

`vibesignal watch` renders a live table in a spare terminal pane, one row per active session, blocked rows first:

```
  PROJECT          AGENT    STATE        FOR
* aegis            claude   * blocked    1m12s
* agent-audit      codex    * blocked    8s
o iet-paper        claude   o done       3s
. random           claude   . working    --
```

Refresh about once a second. ANSI color, standard library only (no new dependency), works in Windows Terminal and on macOS / Linux. It is a foreground viewer the user starts and stops; the USB light stays hook-driven.

### Denied Permission Prompts (fork)

Claude Code fires no hook when the user denies a permission prompt: no `Stop`, no `PostToolUse`, and `PermissionDenied` covers only auto-mode denials. The turn ends silently, so the session would stay `blocked` (8 h backstop). Hooks therefore store the hook's `transcript_path` with the session record, and the resolver treats a `blocked` session as `done` (timestamped at the turn end, so it fades 90 s later) once its transcript records a `system`/`turn_duration` entry after the block. A pending prompt has no such entry and stays blocked. The check reads only the transcript tail, only when the file changed after the block, and is cached per file version. It depends on Claude's transcript format, which is not a documented interface.

## Matter Worker (fork)

`vibesignal matter-sync` runs persistently (a launchd agent on macOS, installed by `install-matter-autostart` together with the local Matter Server). Each tick (0.5 s) it re-reads `matter.json` and `colors.json`, calls `resolve_color()`, and sends commands only when the resolved (config, color) target changes, or every 5 s to retry a failure. Re-reading every tick is what makes color, theme, import, and brightness changes apply without a restart, and what lets lifetime expiry turn the bulb off with no new event. Errors are written to `matter_status.json` and shown by `vibesignal status`.

Rendering is one constant brightness: `moveToLevelWithOnOff` with the level from `brightness_pct` alone, then `moveToHueAndSaturation` with hue and saturation from the state color; idle sends `off`. The RGB value (darkness) of a color deliberately does not affect the level, so every state and theme renders at the same brightness. An earlier fork iteration pulsed to a maximum brightness on priority increases; that was removed in favor of a calm, constant light, and its legacy `max_brightness_pct` / `steady_brightness_pct` / `priority_pulse_seconds` settings migrate to `brightness_pct` (steady, capped at max; else max; else 100).

## Configurable Colors (fork)

`colors.py` is the single source of truth for state colors. The USB light, the widget, and the Matter worker all get colors through `resolve_color()` / `resolve_per_session()`, which read the mapping on every call; the terminal panel keeps fixed basic ANSI colors approximating the defaults. Only the four active states are configurable; `idle` is always off, and black is rejected because a bulb would render it as white. State validity, priority, aliases, and lifetimes stay in `resolve.py` and do not depend on the mapping.

The saved file `~/.vibesignal/colors.json` uses the same versioned document as export (`{"schema_version": 1, "colors": {...}}`). Writers validate the complete mapping before an atomic replace, so a rejected command, theme, or import changes nothing; readers are tolerant and fall back to defaults per state, so a hand-edited file can never crash a hook or the worker. Themes and imports touch only `colors.json`, so brightness and Matter connection settings in `matter.json` are untouched.

## Error Handling

- The panel reuses the store's malformed-file tolerance (`_read_state_file`), so a corrupt session file does not crash the view.
- `watch` traps `KeyboardInterrupt` and restores the cursor and screen on exit.
- A missing or empty store renders an empty table, not an error.

## Testing

- `resolve_per_session()` — unit tests over fake store states: ordering (blocked first), color mapping, TTL filtering, multi-agent rows, and malformed-state tolerance.
- `render(rows, now)` — pure-function tests asserting the formatted output for representative states, including the ANSI color codes and blocked-first ordering (no terminal needed).
- Taxonomy and aggregate tests for the blocked/done split and the `needs_input` alias.

## Future Work: Physical Multi-LED Strip

When the user has a strip (for example a BlinkStick Strip, 8 LEDs), add a `session -> cell` mapping (recommended: an explicit per-terminal `VIBECODING_CELL`, with a project-based fallback) and a multi-LED driver in `light.py`. Both consume `resolve_per_session()` directly, so the strip is additive, not a rewrite.
