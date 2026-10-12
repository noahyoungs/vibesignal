"""Configurable state colors: the single source of truth for every renderer.

The mapping covers the four active states. `idle` is always off and is not
configurable. Colors persist in ``~/.vibesignal/colors.json`` using the same
versioned document that ``vibesignal config --export-colors`` writes, so an
exported file and the saved configuration are interchangeable:

    {"schema_version": 1, "colors": {"working": "#00C83C", ...}}

Readers are tolerant (a missing or corrupt file falls back to the defaults per
state, so a hook or the Matter worker never crashes on a bad edit). Writers are
strict (the whole mapping is validated before an atomic replace), so an invalid
command or import never changes what is saved. Nothing here knows about state
priority or lifetimes; those stay in resolve.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import store

SCHEMA_VERSION = 1

# Active states, in aggregate priority order (highest first) for display.
ACTIVE_STATES = ("blocked", "error", "done", "working")

DEFAULT_COLORS = {
    "working": "#00C83C",  # green
    "blocked": "#DC2626",  # red: needs you now
    "done": "#005AFF",     # blue: your move
    "error": "#9333EA",    # violet: failure, distinct from blocked
}

# Built-in presets. A Matter bulb only takes hue and saturation from these;
# brightness is configured separately (`vibesignal matter-config --brightness`).
THEMES = {
    "default": dict(DEFAULT_COLORS),
    "calm": {  # softer, lower-saturation versions of the default hues
        "working": "#6DBE8C",
        "blocked": "#E07A72",
        "done": "#6F9EE0",
        "error": "#A88BDB",
    },
    "warm": {  # gold, orange, rose-red, magenta
        "working": "#E8C000",
        "blocked": "#DB1F4A",
        "done": "#F2661B",
        "error": "#A3179E",
    },
    "cool": {  # teal, blue, indigo, pink
        "working": "#14B8A6",
        "blocked": "#EC4899",
        "done": "#3B82F6",
        "error": "#8B5CF6",
    },
}

_HEX = re.compile(r"#?([0-9a-fA-F]{6})")


class ColorConfigError(ValueError):
    """A color command, theme, or import was rejected; nothing was changed."""


def colors_path() -> Path:
    return store.base_dir() / "colors.json"


def parse_hex(value: object, state: str) -> str:
    """Validate one active-state color and normalize it to ``#RRGGBB``."""
    match = _HEX.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise ColorConfigError(
            f"invalid color {value!r} for {state}: expected a hex color like #00C83C"
        )
    normalized = "#" + match.group(1).upper()
    if normalized == "#000000":
        # A bulb takes only hue/saturation from the color, so black would light
        # up white. "Off" is reserved for idle.
        raise ColorConfigError(
            f"black is not allowed for {state}: a bulb would show it as white "
            "(only idle is off)"
        )
    return normalized


def _check_state(state: object) -> str:
    if state == "idle":
        raise ColorConfigError("idle is always off and has no color")
    if state not in ACTIVE_STATES:
        raise ColorConfigError(
            f"unknown state {state!r}: expected one of {', '.join(sorted(ACTIVE_STATES))}"
        )
    return state


def validate_mapping(colors: object) -> dict[str, str]:
    """Validate a complete mapping: every active state, nothing else."""
    if not isinstance(colors, dict):
        raise ColorConfigError('"colors" must be an object mapping state names to hex colors')
    for state in colors:
        _check_state(state)
    missing = [s for s in ACTIVE_STATES if s not in colors]
    if missing:
        raise ColorConfigError(f"missing colors for: {', '.join(sorted(missing))}")
    return {state: parse_hex(colors[state], state) for state in ACTIVE_STATES}


def parse_document(data: object) -> dict[str, str]:
    """Validate a versioned color document (export file or saved config)."""
    if not isinstance(data, dict):
        raise ColorConfigError("color file must be a JSON object")
    unknown = sorted(set(data) - {"schema_version", "colors"})
    if unknown:
        raise ColorConfigError(
            f"unsupported keys {', '.join(unknown)}: color files contain only "
            "schema_version and colors"
        )
    version = data.get("schema_version")
    if version != SCHEMA_VERSION or isinstance(version, bool):
        raise ColorConfigError(
            f"unsupported schema_version {version!r}: this version reads {SCHEMA_VERSION}"
        )
    return validate_mapping(data.get("colors"))


def document(colors: dict[str, str]) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "colors": {state: colors[state] for state in DEFAULT_COLORS},
    }
    return json.dumps(payload, indent=2) + "\n"


def read_saved() -> tuple[dict[str, str], str | None]:
    """Return (effective colors, warning). Invalid entries fall back to defaults
    one state at a time; the warning says why, for `config --show`."""
    effective = dict(DEFAULT_COLORS)
    try:
        data = json.loads(colors_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return effective, None
    except (OSError, json.JSONDecodeError) as exc:
        return effective, f"ignoring unreadable {colors_path()}: {exc}"
    try:
        return parse_document(data), None
    except ColorConfigError as exc:
        warning = f"{colors_path()}: {exc}; using defaults where invalid"
    saved = data.get("colors") if isinstance(data, dict) else None
    if isinstance(saved, dict):
        for state in ACTIVE_STATES:
            try:
                effective[state] = parse_hex(saved.get(state), state)
            except ColorConfigError:
                pass
    return effective, warning


def load_colors() -> dict[str, str]:
    """Effective hex colors for the active states (never raises)."""
    return read_saved()[0]


def hex_to_rgb(value: str) -> list[int]:
    return [int(value[i:i + 2], 16) for i in (1, 3, 5)]


def rgb_to_hex(rgb: list | tuple) -> str:
    return "#" + "".join(f"{max(0, min(255, int(v))):02X}" for v in rgb)


def load_rgb() -> dict[str, list | None]:
    """Effective RGB per state, including ``idle: None`` (off)."""
    rgb: dict[str, list | None] = {s: hex_to_rgb(h) for s, h in load_colors().items()}
    rgb["idle"] = None
    return rgb


def save_colors(colors: dict[str, str]) -> dict[str, str]:
    """Validate the full mapping, then replace the saved file atomically."""
    validated = validate_mapping(colors)
    store._atomic_write(colors_path(), document(validated))
    return validated


def set_colors(updates: dict[str, str]) -> dict[str, str]:
    """Change some states, keeping the effective color of every other state."""
    colors = load_colors()
    for state, value in updates.items():
        _check_state(state)
        colors[state] = parse_hex(value, state)
    return save_colors(colors)


def apply_theme(name: str) -> dict[str, str]:
    key = name.strip().lower()
    if key not in THEMES:
        raise ColorConfigError(
            f"unknown theme {name!r}: expected one of {', '.join(THEMES)}"
        )
    return save_colors(THEMES[key])


def reset_colors() -> dict[str, str]:
    return save_colors(DEFAULT_COLORS)


def theme_name(colors: dict[str, str]) -> str | None:
    """The built-in theme this mapping matches exactly, if any."""
    for name, theme in THEMES.items():
        if theme == colors:
            return name
    return None


def export_colors(path: Path) -> dict[str, str]:
    colors = load_colors()
    store._atomic_write(Path(path), document(colors))
    return colors


def import_colors(path: Path) -> dict[str, str]:
    """Validate the whole file first; only a valid file replaces the mapping."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ColorConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise ColorConfigError(f"{path} is not valid JSON: {exc}") from exc
    return save_colors(parse_document(data))
