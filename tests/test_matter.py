import json

import pytest

from vibesignal import colors, matter, store


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.timeouts = []
        self.responses = [json.dumps({"schema_version": 13})]

    def send(self, value):
        request = json.loads(value)
        self.sent.append(request)
        self.responses.append(json.dumps({"message_id": request["message_id"], "result": {}}))

    def recv(self, timeout=None):
        self.timeouts.append(timeout)
        return self.responses.pop(0)

    def close(self):
        pass


class _Recorder:
    def __init__(self):
        self.sent = []

    def command(self, command, args=None, timeout=10):
        assert command == "device_command"
        self.sent.append(args)


def _apply(config, rgb):
    recorder = _Recorder()
    matter.apply_color(recorder, config, rgb)
    return recorder.sent


def _level(args):
    return next(a["payload"]["level"] for a in args if a["cluster_id"] == 8)


def _hue_sat(args):
    payload = next(a["payload"] for a in args if a["cluster_id"] == 768)
    return payload["hue"], payload["saturation"]


def test_apply_color_uses_standard_level_and_color_clusters():
    socket = FakeSocket()
    with matter.MatterClient("ws://example", connector=lambda *_a, **_k: socket) as client:
        matter.apply_color(client, matter.MatterConfig(node_id=7, endpoint_id=2), [255, 0, 0])
    assert {r["command"] for r in socket.sent} == {"device_command"}
    sent = [r["args"] for r in socket.sent]
    assert [a["cluster_id"] for a in sent] == [8, 768]
    assert sent[0]["command_name"] == "moveToLevelWithOnOff"
    assert sent[1]["command_name"] == "moveToHueAndSaturation"
    assert {(a["node_id"], a["endpoint_id"]) for a in sent} == {(7, 2)}
    assert _hue_sat(sent) == (0, 254)


def test_level_comes_only_from_brightness_not_rgb_intensity():
    config = matter.MatterConfig(node_id=7, brightness_pct=12)
    # Same hue at very different RGB intensities, plus dark and pastel colors.
    samples = [[255, 0, 0], [60, 0, 0], [220, 38, 38], [0, 90, 255], [10, 20, 40], [224, 240, 255]]
    levels = {_level(_apply(config, rgb)) for rgb in samples}
    assert levels == {matter.brightness_level(12)} == {30}


def test_every_theme_and_state_commands_the_same_level():
    config = matter.MatterConfig(node_id=7, brightness_pct=40)
    levels, hues = set(), set()
    for theme in colors.THEMES.values():
        for hex_color in theme.values():
            sent = _apply(config, colors.hex_to_rgb(hex_color))
            levels.add(_level(sent))
            hues.add(_hue_sat(sent))
    assert levels == {matter.brightness_level(40)}
    assert len(hues) == 16  # every state in every theme is a distinct hue/sat


def test_brightness_level_range():
    assert matter.brightness_level(1) == 3
    assert matter.brightness_level(100) == 254


def test_apply_none_turns_only_configured_bulb_off():
    sent = _apply(matter.MatterConfig(node_id=9), None)
    assert len(sent) == 1
    args = sent[0]
    assert (args["node_id"], args["endpoint_id"], args["cluster_id"], args["command_name"]) == (9, 1, 6, "off")


def test_config_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    expected = matter.MatterConfig(
        url="ws://host/ws", node_id=3, endpoint_id=4, transition_time=2, brightness_pct=18,
    )
    matter.save_config(expected)
    assert matter.load_config() == expected


def test_fresh_install_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    assert matter.load_config() == matter.MatterConfig()
    assert matter.MatterConfig().brightness_pct == 100


@pytest.mark.parametrize("raw, expected", [
    ({"max_brightness_pct": 20, "steady_brightness_pct": 12, "priority_pulse_seconds": 0.6}, 12),
    ({"max_brightness_pct": 20, "steady_brightness_pct": 50}, 20),   # steady capped at old max
    ({"max_brightness_pct": 35}, 35),                                 # no steady: old max
    ({"max_brightness_pct": 35, "steady_brightness_pct": None}, 35),
    ({"max_brightness_pct": 35, "steady_brightness_pct": "bad"}, 35),
    ({"max_brightness_pct": 35, "steady_brightness_pct": 0}, 35),
    ({"max_brightness_pct": 500, "steady_brightness_pct": 12}, 12),  # invalid max ignored
    ({"priority_pulse_seconds": 2}, 100),                             # pulse alone ignored
    ({}, 100),
    ({"brightness_pct": 7, "max_brightness_pct": 20, "steady_brightness_pct": 12}, 7),
    ({"brightness_pct": 0, "max_brightness_pct": 20}, 20),            # invalid new value
])
def test_legacy_brightness_migration(raw, expected):
    assert matter.migrate_brightness(raw) == expected


def test_migration_preserves_connection_and_unrelated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    legacy = {
        "url": "ws://10.0.0.2:5580/ws", "node_id": 4, "endpoint_id": 2, "transition_time": 5,
        "max_brightness_pct": 20, "steady_brightness_pct": 12, "priority_pulse_seconds": 0.6,
        "note": "kept",
    }
    matter.config_path().write_text(json.dumps(legacy), encoding="utf-8")
    loaded = matter.load_config()
    assert loaded == matter.MatterConfig(
        url="ws://10.0.0.2:5580/ws", node_id=4, endpoint_id=2, transition_time=5, brightness_pct=12,
    )
    matter.save_config(loaded)
    first = json.loads(matter.config_path().read_text(encoding="utf-8"))
    assert first["brightness_pct"] == 12 and first["note"] == "kept"
    assert not set(first) & {"max_brightness_pct", "steady_brightness_pct", "priority_pulse_seconds"}
    # Repeatable: loading and saving the migrated file changes nothing.
    matter.save_config(matter.load_config())
    assert json.loads(matter.config_path().read_text(encoding="utf-8")) == first


def test_corrupt_fields_fall_back_instead_of_crashing_the_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    matter.config_path().write_text(json.dumps({
        "node_id": "abc", "endpoint_id": "x", "transition_time": -1, "brightness_pct": True,
    }), encoding="utf-8")
    assert matter.load_config() == matter.MatterConfig()


def test_commission_allows_time_for_network_discovery():
    socket = FakeSocket()
    with matter.MatterClient("ws://example", connector=lambda *_a, **_k: socket) as client:
        client.command("commission_with_code", {"code": "12345678901"}, timeout=120)
    assert socket.timeouts == [3, 120]


# -- run_sync with mocked Matter I/O -----------------------------------------

class _StopLoop(Exception):
    pass


class FakeClient:
    batches: list = []

    def __init__(self, url, connector=None):
        self.url = url

    def __enter__(self):
        FakeClient.batches.append([])
        return self

    def __exit__(self, *_exc):
        return False

    def command(self, command, args=None, timeout=10):
        FakeClient.batches[-1].append(args)


def _run_ticks(monkeypatch, steps, interval=0.5):
    """Run the worker loop, executing steps[i] between tick i and i+1.
    Returns (one list of commands per tick, every sleep duration)."""
    FakeClient.batches = []
    monkeypatch.setattr(matter, "MatterClient", FakeClient)
    per_tick, sleeps = [], []
    remaining = list(steps)

    def fake_sleep(seconds):
        sleeps.append(seconds)
        per_tick.append([c for batch in FakeClient.batches for c in batch])
        FakeClient.batches.clear()
        if not remaining:
            raise _StopLoop
        remaining.pop(0)()

    monkeypatch.setattr(matter.time, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        matter.run_sync(interval=interval)
    return per_tick, sleeps


@pytest.fixture
def bulb(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    matter.save_config(matter.MatterConfig(node_id=7, brightness_pct=12))


def test_no_pulse_on_startup_or_priority_changes(bulb, monkeypatch):
    store.record("claude", "s1", "working")
    steps = [
        lambda: store.record("codex", "s2", "done"),
        lambda: store.record("opencode", "s3", "error"),
        lambda: store.record("claude", "s1", "blocked"),
        lambda: store.clear(agent="claude"),           # priority drops
    ]
    per_tick, sleeps = _run_ticks(monkeypatch, steps)
    assert sleeps == [0.5] * 5  # no pulse/settle delays, only the poll interval
    for commands in per_tick:
        # Exactly one level + one color command per change: no peak, no settle.
        assert [c["cluster_id"] for c in commands] == [8, 768]
        assert _level(commands) == matter.brightness_level(12)
    expected = ["working", "done", "error", "blocked", "error"]
    rgb = colors.load_rgb()
    for commands, state in zip(per_tick, expected):
        sent = _apply(matter.MatterConfig(node_id=7), rgb[state])
        assert _hue_sat(commands) == _hue_sat(sent)


def test_idle_turns_bulb_off(bulb, monkeypatch):
    store.record("claude", "s1", "working")
    per_tick, _ = _run_ticks(monkeypatch, [lambda: store.clear()])
    assert [c["cluster_id"] for c in per_tick[0]] == [8, 768]
    assert [(c["cluster_id"], c["command_name"]) for c in per_tick[1]] == [(6, "off")]
    assert store.get_matter_status()["color"] is None


def test_unchanged_target_sends_nothing(bulb, monkeypatch):
    store.record("claude", "s1", "working")
    per_tick, _ = _run_ticks(monkeypatch, [lambda: None, lambda: None])
    assert len(per_tick[0]) == 2
    assert per_tick[1] == [] and per_tick[2] == []


def test_live_color_and_brightness_updates_without_restart(bulb, monkeypatch):
    store.record("claude", "s1", "working")

    def set_brightness():
        matter.save_config(matter.MatterConfig(node_id=7, brightness_pct=60))

    steps = [lambda: colors.apply_theme("warm"), set_brightness]
    per_tick, _ = _run_ticks(monkeypatch, steps)
    default_hs, warm_hs = _hue_sat(per_tick[0]), _hue_sat(per_tick[1])
    assert default_hs != warm_hs
    # A theme change keeps the brightness; a brightness change keeps the hue.
    assert _level(per_tick[0]) == _level(per_tick[1]) == matter.brightness_level(12)
    assert _level(per_tick[2]) == matter.brightness_level(60)
    assert _hue_sat(per_tick[2]) == warm_hs


def test_failed_apply_records_error_and_retries(bulb, monkeypatch):
    store.record("claude", "s1", "working")

    class Boom(FakeClient):
        def __enter__(self):
            raise OSError("server down")

    monkeypatch.setattr(matter, "MatterClient", Boom)
    monkeypatch.setattr(matter.time, "sleep", lambda _s: None)
    matter.run_sync(once=True)
    assert store.get_matter_status()["error"] == "server down"
