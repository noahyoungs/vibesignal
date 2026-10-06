# Wiring Codex Into the Signal Light

The signal light is agent-agnostic. Every state event carries an `--agent` tag, so
Codex drives the same light as Claude Code by calling:

```bash
vibesignal event --agent codex --state working --quiet
vibesignal event --agent codex --state blocked --quiet
```

`needs_input` is still accepted as an alias for `blocked`, so a v1 wrapper keeps
working.

## Quick Setup

One command wires everything, with the Codex-correct events and the absolute
path pinned (so a minimal hook `PATH` cannot break it):

```bash
vibesignal install-hooks --agent codex
```

It merges the per-turn hooks into `~/.codex/hooks.json`, preserving any hooks you
already have. Then trust them once via `/hooks` in a Codex session and restart it.
The rest of this document explains the mapping that command applies.

The intended mapping mirrors the Claude Code side:

| Codex situation | State |
|-----------------|-------|
| A turn starts, Codex is working | `working` |
| Codex needs approval or input | `blocked` |
| A turn finishes and waits on you | `done` |

## Where to Hook In

Codex exposes a `notify` program in `~/.codex/config.toml`, and newer versions add
a hooks system. The `notify` program is called with a JSON argument when events
such as "turn complete" or "approval required" occur. Point it at a small wrapper
that maps the event to a `vibesignal` call.

Exact event names and the JSON shape vary by Codex version, so confirm them against
the Codex docs for your installed version (`codex --version`) before relying on the
mapping. The command form above is stable; only the event source differs.

A minimal wrapper (pseudocode, adjust the event keys to your Codex version):

```python
# codex-notify.py  (set as the notify program in ~/.codex/config.toml)
import json, subprocess, sys

event = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
kind = event.get("type", "")

state = "working"
if kind == "approval-requested":
    state = "blocked"
elif kind == "agent-turn-complete":
    state = "done"

subprocess.run([
    "vibesignal", "event", "--agent", "codex",
    "--state", state, "--session", event.get("session_id", "codex"),
])
```

## Per-Turn States via the Hooks System

Codex 0.130+ exposes a hooks system that fires on the same per-turn events Claude
Code uses, configured in `~/.codex/config.toml` or an isolated `~/.codex/hooks.json`.
Map `UserPromptSubmit` and `PostToolUse` to `working`, `PermissionRequest` to
`blocked`, `Stop` to `done`, and `SessionEnd` to `end` for the same live states as the
Claude side. Trust the
hooks once via `/hooks` in a Codex session. Use `--quiet` for Codex hooks: some hook
types parse stdout as JSON, so normal human-readable status text causes an invalid
hook output error.

The tracked snippet is [`codex-hooks.snippet.json`](codex-hooks.snippet.json). Merge it
into `~/.codex/hooks.json`. If `vibesignal` is not on the hook shell's `PATH`, replace
the command prefix with an absolute interpreter form such as
`C:/Users/<you>/miniforge3/envs/py312/python.exe -m vibesignal`.

Keep [`codex-notify.py`](codex-notify.py) as a completion-only fallback for older Codex
builds or for environments where the hooks system is disabled.

## SessionEnd

Current Codex releases fire `SessionEnd`, so `install-hooks --agent codex` wires it
to `vibesignal end --agent codex --quiet` and a closed chat leaves the light at once,
as on the Claude side. On an older Codex without that event, a closed session ages out
by its per-state lifetime instead: `done` after 90 s, `error` after 5 min, `working`
after 10 min, `blocked` at the 8 h backstop.

This keeps the Claude Code and Codex paths on one light and one state store, which
satisfies the cross-agent requirement: the function works under role reversal or
when only one agent is present.
