"""Matter bulb renderer backed by the OHF Matter Server WebSocket API.

Hooks only write the shared session store. This module runs as a small worker,
resolves all live sessions, and is the sole writer to the network bulb. Keeping
Matter I/O out of hooks makes agent events fast and gives TTL expiry a process
that can actually turn the bulb off.
"""

from __future__ import annotations

import colorsys
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import store
from .resolve import resolve_color


DEFAULT_URL = "ws://127.0.0.1:5580/ws"


# Keys from the retired max/steady/pulse model. load_config reads them once to
# derive `brightness_pct`; save_config drops them.
_LEGACY_KEYS = ("max_brightness_pct", "steady_brightness_pct", "priority_pulse_seconds")


@dataclass(frozen=True)
class MatterConfig:
    url: str = DEFAULT_URL
    node_id: int | None = None
    endpoint_id: int = 1
    transition_time: int = 3  # tenths of a second
    brightness_pct: int = 100  # one constant level for every active state


def config_path() -> Path:
    return store.base_dir() / "matter.json"


def _int_in(value: object, low: int, high: int) -> int | None:
    """An int (or digit string) within [low, high], else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) and low <= value <= high else None


def _read_raw() -> dict:
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def migrate_brightness(raw: dict) -> int:
    """Brightness from a saved config, new or legacy.

    `brightness_pct` wins when valid. Otherwise a legacy steady brightness is
    kept (capped at the legacy maximum), then the legacy maximum, then 100.
    Pulse settings are ignored. Pure, so loading is repeatable.
    """
    current = _int_in(raw.get("brightness_pct"), 1, 100)
    if current is not None:
        return current
    maximum = _int_in(raw.get("max_brightness_pct"), 1, 100) or 100
    steady = _int_in(raw.get("steady_brightness_pct"), 1, 100)
    return min(steady, maximum) if steady is not None else maximum


def load_config() -> MatterConfig:
    raw = _read_raw()
    defaults = MatterConfig()
    endpoint = _int_in(raw.get("endpoint_id"), 0, 0xFFFF)
    transition = _int_in(raw.get("transition_time"), 0, 0xFFFF)
    return MatterConfig(
        url=str(raw.get("url") or DEFAULT_URL),
        node_id=_int_in(raw.get("node_id"), 1, 2**64 - 1),
        endpoint_id=defaults.endpoint_id if endpoint is None else endpoint,
        transition_time=defaults.transition_time if transition is None else transition,
        brightness_pct=migrate_brightness(raw),
    )


def save_config(config: MatterConfig) -> None:
    """Write the config, keeping unrelated keys and dropping legacy ones."""
    payload = {k: v for k, v in _read_raw().items() if k not in _LEGACY_KEYS}
    payload.update({
        "url": config.url,
        "node_id": config.node_id,
        "endpoint_id": config.endpoint_id,
        "transition_time": config.transition_time,
        "brightness_pct": config.brightness_pct,
    })
    store._atomic_write(config_path(), json.dumps(payload, indent=2) + "\n")


class MatterError(RuntimeError):
    pass


class MatterClient:
    def __init__(self, url: str, connector: Callable | None = None):
        self.url = url
        self._connector = connector
        self._socket = None

    def __enter__(self):
        connector = self._connector
        if connector is None:
            from websockets.sync.client import connect
            connector = connect
        self._socket = connector(self.url, open_timeout=3, close_timeout=1)
        json.loads(self._socket.recv(timeout=3))  # unsolicited server_info
        return self

    def __exit__(self, *_exc):
        if self._socket is not None:
            self._socket.close()

    def command(self, command: str, args: dict | None = None, timeout: float = 10):
        if self._socket is None:
            raise MatterError("Matter client is not connected")
        message_id = uuid.uuid4().hex
        payload = {"message_id": message_id, "command": command}
        if args is not None:
            payload["args"] = args
        self._socket.send(json.dumps(payload))
        while True:
            response = json.loads(self._socket.recv(timeout=timeout))
            if response.get("message_id") != message_id:
                continue
            if "error_code" in response:
                raise MatterError(response.get("details") or f"Matter error {response['error_code']}")
            return response.get("result")


def _device_command(config: MatterConfig, cluster: int, name: str, payload: dict) -> dict:
    if config.node_id is None:
        raise MatterError("Matter node is not configured")
    return {
        "node_id": config.node_id,
        "endpoint_id": config.endpoint_id,
        "cluster_id": cluster,
        "command_name": name,
        "payload": payload,
    }


def apply_color(client: MatterClient, config: MatterConfig, rgb: list | None) -> None:
    """Show `rgb` on an Extended Color Light, or turn it off for None (idle).

    Only hue and saturation come from the color. The level comes solely from
    `config.brightness_pct`, so every active state (and every theme) renders at
    the same brightness regardless of how dark or light its RGB value is.
    """
    if rgb is None:
        client.command("device_command", _device_command(config, 6, "off", {}))
        return

    red, green, blue = (max(0, min(255, int(v))) / 255 for v in rgb)
    hue, saturation, _value = colorsys.rgb_to_hsv(red, green, blue)
    transition = config.transition_time
    client.command("device_command", _device_command(config, 8, "moveToLevelWithOnOff", {
        "level": brightness_level(config.brightness_pct),
        "transitionTime": transition,
        "optionsMask": {},
        "optionsOverride": {},
    }))
    client.command("device_command", _device_command(config, 768, "moveToHueAndSaturation", {
        "hue": round(hue * 254) % 255,
        "saturation": round(saturation * 254),
        "transitionTime": transition,
        "optionsMask": {},
        "optionsOverride": {},
    }))


def brightness_level(percent: int) -> int:
    """Matter Level Control value (1-254) for a 1-100 brightness percentage."""
    return max(1, min(254, round(percent * 254 / 100)))


def list_nodes(config: MatterConfig) -> list:
    with MatterClient(config.url) as client:
        result = client.command("get_nodes", {"only_available": False})
    return result if isinstance(result, list) else []


def commission(config: MatterConfig, code: str, network_only: bool = True) -> int:
    """Commission a bulb already placed on Wi-Fi by another Matter controller."""
    with MatterClient(config.url) as client:
        result = client.command(
            "commission_with_code",
            {
                "code": code.strip(),
                "network_only": network_only,
            },
            timeout=120,
        )
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        node = result.get("node_id", result.get("nodeId"))
        if isinstance(node, int):
            return node
    raise MatterError(f"Matter server returned no node id: {result!r}")


def run_sync(interval: float = 0.5, once: bool = False) -> int:
    """Continuously mirror aggregate VibeSignal state to the configured bulb.

    Config and colors are re-read every tick, so `matter-config` and
    `vibesignal config` changes reach the bulb without a restart. A command is
    sent only when the (config, color) target changes, or to retry a failure.
    """
    last_attempted = object()
    retry_at = 0.0
    while True:
        config = load_config()
        _state, color = resolve_color()
        target = (config, tuple(color) if color is not None else None)
        now = time.monotonic()
        if config.node_id is None:
            last_attempted = object()  # re-apply once a node is selected again
            if store.get_matter_status().get("error"):
                store.set_matter_error(None)
        elif target != last_attempted or now >= retry_at:
            try:
                with MatterClient(config.url) as client:
                    apply_color(client, config, color)
                last_attempted = target
                retry_at = float("inf")
                store.set_matter_last_color(color)
                store.set_matter_error(None)
            except Exception as exc:
                retry_at = now + 5
                store.set_matter_error(str(exc))
        if once:
            return 0
        time.sleep(max(0.1, interval))
