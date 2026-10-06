"""Physical status light for coding agents.

Agent hooks report per-session state to a small file store, the aggregate state is
resolved by priority, and renderers show the matching color: a USB busylight (set
directly by hooks), screen viewers, and an optional persistent Matter bulb worker.
See README.md and DESIGN.md.
"""

from .resolve import State, resolve_color

__all__ = ["State", "resolve_color"]
__version__ = "0.1.2"
