"""Skip the overnight grid charge when the car already has enough.

On the nights the user picks (say Friday and Saturday, so the weekend is left
to solar), a car at or above the chosen battery level isn't charged from the
grid: the target drops to the current level, so there is no deficit and the
charger's own Eco-Smart solar fills it by day. Below that level the night
charges as usual.

The charger never knows the car's battery level, so this lives in HA, where the
car's SOC entity is. An unknown level (unavailable entity) charges as usual.

A "night" runs from noon to noon: 00:30 on Saturday is still Friday night, so
a window that starts after midnight belongs to the evening before it.

Pure, HA-free; charge_assistant.py does the I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def night_of(now_local: datetime) -> str:
    """The night a local time belongs to (noon to noon), as "mon".."sun"."""
    return DAYS[(now_local - timedelta(hours=12)).weekday()]


def threshold_of(value: Any) -> float | None:
    """The configured level as a number, or None when off (unset, 0, junk)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 100 else None


def nights_of(value: Iterable[str] | None) -> tuple[str, ...]:
    """The chosen nights; none chosen means every night."""
    picked = tuple(d for d in DAYS if d in set(value or ()))
    return picked or DAYS


def skip_tonight(soc: float | None, threshold: Any, nights: Iterable[str] | None,
                 now_local: datetime) -> bool:
    """True when tonight is a chosen night and the car is at or above the level."""
    level = threshold_of(threshold)
    if level is None or soc is None:
        return False
    return night_of(now_local) in nights_of(nights) and soc >= level
