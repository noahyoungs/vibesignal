import colorsys
import itertools
import json

import pytest

from vibesignal import __main__ as cli
from vibesignal import colors, matter
from vibesignal.resolve import resolve_color


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    monkeypatch.setattr(cli.light, "set_color", lambda rgb: False)  # no USB device
    return tmp_path


def _doc(**overrides):
    data = {"schema_version": 1, "colors": dict(colors.DEFAULT_COLORS)}
    data.update(overrides)
    return data


def test_defaults_match_documented_colors(state_dir):
    assert colors.load_colors() == {
        "working": "#5CCCB9", "blocked": "#F07D9B", "done": "#8B84F0", "error": "#D585E6",
    }
    assert colors.load_rgb()["idle"] is None
    assert not colors.colors_path().exists()  # nothing written until a change


def test_partial_update_persists_and_preserves_other_states(state_dir):
    colors.set_colors({"working": "00aa55"})
    colors.set_colors({"done": "#123ABC"})
    saved = json.loads(colors.colors_path().read_text(encoding="utf-8"))
    assert saved == {"schema_version": 1, "colors": {
        "working": "#00AA55", "blocked": "#F07D9B", "done": "#123ABC", "error": "#D585E6",
    }}
    assert resolve_color()[1] is None  # idle stays off


def test_reset_restores_defaults(state_dir):
    colors.apply_theme("warm")
    colors.set_colors({"error": "#ABCDEF"})
    assert colors.reset_colors() == colors.DEFAULT_COLORS
    assert colors.load_colors() == colors.DEFAULT_COLORS


@pytest.mark.parametrize("state, value, message", [
    ("working", "green", "invalid color"),
    ("working", "#12345", "invalid color"),
    ("working", "#1234567", "invalid color"),
    ("working", "#GGGGGG", "invalid color"),
    ("working", 123, "invalid color"),
    ("error", "#000000", "black is not allowed"),
    ("error", "000000", "black is not allowed"),
    ("idle", "#FFFFFF", "idle is always off"),
    ("needs_input", "#FFFFFF", "unknown state"),
    ("purple", "#FFFFFF", "unknown state"),
])
def test_invalid_input_rejected_without_changes(state_dir, state, value, message):
    colors.set_colors({"done": "#111111"})
    before = colors.colors_path().read_bytes()
    with pytest.raises(colors.ColorConfigError, match=message):
        colors.set_colors({"working": "#00FF00", state: value})
    assert colors.colors_path().read_bytes() == before


def test_theme_then_individual_override(state_dir):
    assert colors.apply_theme("Classic") == colors.THEMES["classic"]
    assert colors.theme_name(colors.load_colors()) == "classic"
    colors.set_colors({"blocked": "#FF0000"})
    effective = colors.load_colors()
    assert effective["blocked"] == "#FF0000"
    assert {s: effective[s] for s in ("working", "done", "error")} == {
        s: colors.THEMES["classic"][s] for s in ("working", "done", "error")
    }
    assert colors.theme_name(effective) is None


def test_calm_theme_name_still_applies_the_calm_default(state_dir):
    colors.apply_theme("classic")
    assert colors.apply_theme("calm") == colors.DEFAULT_COLORS
    assert colors.theme_name(colors.load_colors()) == "default"  # same mapping


def test_unknown_theme_rejected(state_dir):
    with pytest.raises(colors.ColorConfigError, match="unknown theme"):
        colors.apply_theme("neon")
    assert not colors.colors_path().exists()


def test_themes_are_complete_valid_and_distinguishable():
    assert list(colors.THEMES) == ["default", "calm", "classic", "warm", "cool"]
    assert colors.THEMES["default"] == colors.DEFAULT_COLORS
    assert colors.THEMES["calm"] == colors.DEFAULT_COLORS  # calm is the default
    for name, theme in colors.THEMES.items():
        assert colors.validate_mapping(theme) == theme, name
        hsv = {}
        for state, value in theme.items():
            r, g, b = (c / 255 for c in colors.hex_to_rgb(value))
            hue, sat, _v = colorsys.rgb_to_hsv(r, g, b)
            hsv[state] = (hue * 360, sat)
            assert sat >= 0.35, (name, state)  # still clearly colored on a bulb
        for a, b in itertools.combinations(hsv, 2):
            gap = abs(hsv[a][0] - hsv[b][0])
            assert min(gap, 360 - gap) >= 25, (name, a, b)
    calm_sat = max(colorsys.rgb_to_hsv(*(c / 255 for c in colors.hex_to_rgb(v)))[1]
                   for v in colors.THEMES["calm"].values())
    classic_sat = min(colorsys.rgb_to_hsv(*(c / 255 for c in colors.hex_to_rgb(v)))[1]
                      for v in colors.THEMES["classic"].values())
    assert calm_sat < classic_sat  # calm really is softer than the old vivid defaults


def _hue_gap(a: float, b: float) -> float:
    gap = abs(a - b) % 360
    return min(gap, 360 - gap)


# The Matter hue/saturation (0-254) each calm default sends. Brightness is not
# part of this: the level comes only from `brightness_pct`.
CALM_BULB_COMMANDS = {
    "working": (120, 139),  # #5CCCB9 ocean teal,       169.8 deg, s 0.55
    "blocked": (243, 122),  # #F07D9B dawn rose,        344.3 deg, s 0.48
    "done": (172, 114),     # #8B84F0 night periwinkle, 243.9 deg, s 0.45
    "error": (204, 107),    # #D585E6 night orchid,     289.5 deg, s 0.42
}


@pytest.mark.parametrize("theme", ["default", "calm", "classic"])
def test_palette_stays_distinguishable_on_a_matter_bulb(theme):
    # A Matter bulb renders only hue and saturation (moveToHueAndSaturation), so
    # soft colors must still be far apart in hue and saturated enough not to wash
    # out to white.
    palette = colors.THEMES[theme]
    hsv = {}
    for state, value in palette.items():
        r, g, b = (c / 255 for c in colors.hex_to_rgb(value))
        hue, sat, _v = colorsys.rgb_to_hsv(r, g, b)
        hsv[state] = (hue * 360, sat)
        assert sat >= 0.35, (theme, state, sat)
    for a, b in itertools.combinations(("working", "blocked", "done"), 2):
        assert _hue_gap(hsv[a][0], hsv[b][0]) >= 60, (theme, a, b)
    assert _hue_gap(hsv["error"][0], hsv["blocked"][0]) >= 30, theme


def test_calm_defaults_keep_the_calm_chords_hues():
    # Each default only deepens the saturation of a calm-chords tone; the hue is
    # the source's (src/style.css, src/moods.ts).
    sources = {
        "working": "#A8FFF0",  # ocean screen
        "blocked": "#F7B7C8",  # dawn sky rose
        "done": "#C8C4FF",     # deep-night screen
    }
    for state, source in {**sources, "error": None}.items():
        r, g, b = (c / 255 for c in colors.hex_to_rgb(colors.DEFAULT_COLORS[state]))
        hue = colorsys.rgb_to_hsv(r, g, b)[0] * 360
        if source is None:
            target = 290  # deep-night chord hue
        else:
            target = colorsys.rgb_to_hsv(*(c / 255 for c in colors.hex_to_rgb(source)))[0] * 360
        assert _hue_gap(hue, target) <= 1, (state, hue, target)


def test_calm_defaults_send_expected_matter_hue_and_saturation():
    class _Recorder:
        def __init__(self):
            self.sent = []

        def command(self, command, args=None, timeout=10):
            self.sent.append(args)

    config = matter.MatterConfig(node_id=7, brightness_pct=40)
    for state, expected in CALM_BULB_COMMANDS.items():
        recorder = _Recorder()
        matter.apply_color(recorder, config, colors.hex_to_rgb(colors.DEFAULT_COLORS[state]))
        level = next(a["payload"] for a in recorder.sent if a["cluster_id"] == 8)
        color = next(a["payload"] for a in recorder.sent if a["cluster_id"] == 768)
        assert (color["hue"], color["saturation"]) == expected, state
        assert color["saturation"] >= round(0.35 * 254)
        assert level["level"] == matter.brightness_level(40)  # brightness unchanged
    # The quantized hues the bulb receives keep the same separation.
    hues = {s: h * 360 / 254 for s, (h, _sat) in CALM_BULB_COMMANDS.items()}
    for a, b in itertools.combinations(("working", "blocked", "done"), 2):
        assert _hue_gap(hues[a], hues[b]) >= 60, (a, b)
    assert _hue_gap(hues["error"], hues["blocked"]) >= 30


def test_export_import_round_trip_includes_overrides(state_dir):
    colors.apply_theme("cool")
    colors.set_colors({"done": "#0A0B0C"})
    path = state_dir / "desk-colors.json"
    exported = colors.export_colors(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"schema_version": 1, "colors": exported}
    assert list(data["colors"]) == ["working", "blocked", "done", "error"]
    assert data["colors"]["done"] == "#0A0B0C"
    colors.reset_colors()
    assert colors.import_colors(path) == exported
    assert colors.load_colors() == exported


@pytest.mark.parametrize("payload, message", [
    ("not json", "not valid JSON"),
    ([], "must be a JSON object"),
    (_doc(schema_version=2), "unsupported schema_version 2"),
    (_doc(schema_version="1"), "unsupported schema_version"),
    (_doc(schema_version=True), "unsupported schema_version"),
    ({"colors": colors.DEFAULT_COLORS}, "unsupported schema_version None"),
    (_doc(brightness_pct=12), "unsupported keys brightness_pct"),
    (_doc(colors=[]), "must be an object"),
    (_doc(colors={"working": "#00C83C"}), "missing colors for: blocked, done, error"),
    (_doc(colors={**colors.DEFAULT_COLORS, "idle": "#FFFFFF"}), "idle is always off"),
    (_doc(colors={**colors.DEFAULT_COLORS, "thinking": "#FFFFFF"}), "unknown state 'thinking'"),
    (_doc(colors={**colors.DEFAULT_COLORS, "done": "blue"}), "invalid color 'blue' for done"),
    (_doc(colors={**colors.DEFAULT_COLORS, "error": "#000000"}), "black is not allowed"),
])
def test_invalid_import_leaves_settings_intact(state_dir, payload, message):
    colors.apply_theme("warm")
    before = colors.colors_path().read_bytes()
    path = state_dir / "bad.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    with pytest.raises(colors.ColorConfigError, match=message):
        colors.import_colors(path)
    assert colors.colors_path().read_bytes() == before


def test_missing_import_file_has_useful_error(state_dir):
    with pytest.raises(colors.ColorConfigError, match="cannot read"):
        colors.import_colors(state_dir / "nope.json")


def test_corrupt_saved_file_falls_back_per_state(state_dir):
    colors.colors_path().parent.mkdir(parents=True, exist_ok=True)
    colors.colors_path().write_text(json.dumps(
        {"schema_version": 1, "colors": {"working": "#ABCDEF", "done": "nope"}}
    ), encoding="utf-8")
    effective, warning = colors.read_saved()
    assert effective == {**colors.DEFAULT_COLORS, "working": "#ABCDEF"}
    assert "missing colors" in warning
    colors.colors_path().write_text("{broken", encoding="utf-8")
    assert colors.load_colors() == colors.DEFAULT_COLORS  # hooks never crash on it


def test_themes_and_imports_preserve_brightness_and_connection(state_dir):
    config = matter.MatterConfig(url="ws://10.0.0.2:5580/ws", node_id=4, endpoint_id=2,
                                 transition_time=5, brightness_pct=12)
    matter.save_config(config)
    before = matter.config_path().read_bytes()
    path = state_dir / "x.json"
    cli.main(["config", "--theme", "cool"])
    cli.main(["config", "--export-colors", str(path)])
    cli.main(["config", "--import-colors", str(path)])
    cli.main(["config", "--color", "done", "#445566"])
    cli.main(["config", "--reset-colors"])
    assert matter.config_path().read_bytes() == before
    assert matter.load_config() == config


# -- CLI ----------------------------------------------------------------------

def test_cli_show_is_the_default(state_dir, capsys):
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "theme: default" in out and "#5CCCB9" in out and "idle     off" in out
    assert "brightness=100%" in out


def test_cli_color_updates_and_reapplies_usb_light(state_dir, capsys, monkeypatch):
    applied = []
    monkeypatch.setattr(cli.light, "set_color", lambda rgb: applied.append(rgb) or True)
    from vibesignal import store
    store.record("claude", "s1", "working")
    cli.main(["config", "--color", "working", "#112233", "--color", "done", "#445566"])
    assert colors.load_colors()["working"] == "#112233"
    assert colors.load_colors()["done"] == "#445566"
    assert applied == [[0x11, 0x22, 0x33]]  # the USB light follows without a hook event


def test_cli_list_and_apply_theme(state_dir, capsys):
    cli.main(["config", "--theme", "warm"])
    capsys.readouterr()
    cli.main(["config", "--list-themes"])
    out = capsys.readouterr().out
    assert "* warm" in out and "  calm" in out and colors.THEMES["cool"]["done"] in out


def test_cli_errors_exit_nonzero_with_message(state_dir):
    with pytest.raises(SystemExit, match="black is not allowed"):
        cli.main(["config", "--color", "working", "#000000"])
    with pytest.raises(SystemExit, match="unknown theme"):
        cli.main(["config", "--theme", "neon"])
    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "--theme", "warm", "--reset-colors"])  # mutually exclusive
    assert exc.value.code == 2
    assert not colors.colors_path().exists()


def test_cli_matter_brightness_validation_and_partial_update(state_dir, capsys):
    matter.save_config(matter.MatterConfig(url="ws://h/ws", node_id=4, brightness_pct=50))
    for bad in ("0", "101", "12.5", "abc"):
        with pytest.raises(SystemExit) as exc:
            cli.main(["matter-config", "--brightness", bad])
        assert exc.value.code == 2
    cli.main(["matter-config", "--brightness", "12"])
    assert matter.load_config() == matter.MatterConfig(url="ws://h/ws", node_id=4, brightness_pct=12)
    assert "brightness=12%" in capsys.readouterr().out


def test_cli_matter_config_without_flags_migrates_legacy_file(state_dir):
    matter.config_path().parent.mkdir(parents=True, exist_ok=True)
    matter.config_path().write_text(json.dumps({
        "node_id": 4, "max_brightness_pct": 20, "steady_brightness_pct": 12,
        "priority_pulse_seconds": 0.6,
    }), encoding="utf-8")
    cli.main(["matter-config"])
    saved = json.loads(matter.config_path().read_text(encoding="utf-8"))
    assert saved["brightness_pct"] == 12 and saved["node_id"] == 4
    assert "priority_pulse_seconds" not in saved


def test_cli_removed_pulse_flags_are_rejected(state_dir):
    for flag in ("--max-brightness", "--steady-brightness", "--priority-pulse-seconds"):
        with pytest.raises(SystemExit) as exc:
            cli.main(["matter-config", flag, "10"])
        assert exc.value.code == 2


def test_readme_theme_preview_matches_builtin_themes():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    svg = (root / "docs" / "themes.svg").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    for theme in colors.THEMES.values():
        for value in theme.values():
            assert value in svg and value in readme
