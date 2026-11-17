"""Keep the car at or above the floor at any time, not only overnight.

The solar planner's floor (default 50 %) is always charged overnight. This
covers the rest of the day: a car plugged in below the floor outside the cheap
window is charged to the floor straight away, unless the cheap window starts
within WAIT_FOR_WINDOW_MIN (then the normal overnight plan takes it from there).
At the floor the charge stops and the charger's own solar takes over again.

Pure, HA-free; charge_assistant.py does the I/O.
"""

from __future__ import annotations

from typing import Any

WAIT_FOR_WINDOW_MIN = 120

NONE = "none"
START = "start"   # start a grid charge to the floor
HOLD = "hold"     # our floor charge is running: let it run (skip the window stop)
STOP = "stop"     # reached the floor: stop and hand back


def floor_of(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 100 else None


def decide(*, floor: Any, soc: float | None, charging: bool, plugged: bool | None,
           floor_charging: bool, mins_to_window: int | None) -> str:
    """mins_to_window: 0 inside the cheap window, minutes until it starts,
    or None when there is no window."""
    level = floor_of(floor)
    if floor_charging:
        if level is None or plugged is False or soc is None:
            return STOP
        if soc >= level:
            return STOP
        return HOLD if charging else NONE
    if level is None or soc is None or charging or plugged is not True:
        return NONE
    if soc >= level:
        return NONE
    if mins_to_window is not None and mins_to_window <= WAIT_FOR_WINDOW_MIN:
        return NONE   # in the window, or it starts soon: the normal plan charges
    return START
