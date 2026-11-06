"""Window strings to seconds, and back. One parser, so "28d" can't mean two different
things in the rules and in the report."""

from __future__ import annotations

import re

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_PATTERN = re.compile(r"^(\d+)([smhdw])$")


class WindowError(ValueError):
    """Raised for a window that cannot be parsed."""


def parse_window(text: str) -> int:
    """Parse "90s" / "5m" / "6h" / "28d" into seconds."""
    if not isinstance(text, str):
        raise WindowError(f"window must be a string like '5m', got {type(text).__name__}")
    match = _PATTERN.match(text.strip().lower())
    if not match:
        raise WindowError(f"cannot parse window {text!r}; expected e.g. '30s', '5m', '6h', '28d'")
    amount, unit = int(match.group(1)), match.group(2)
    if amount <= 0:
        raise WindowError(f"window {text!r} must be positive")
    return amount * _UNITS[unit]


def format_window(seconds: int) -> str:
    """Seconds back to the shortest form. Days and down only: windows here are written in
    days, so 28 days has to print as "28d" and not "4w"."""
    for unit in ("d", "h", "m", "s"):
        size = _UNITS[unit]
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
