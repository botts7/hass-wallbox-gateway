"""Stop free solar from filling the car past a cap (Smart charge, hand back to solar).

With "Hand back to solar" on, the target is only the overnight grid floor:
between top-ups the charger's own Eco-Smart solar keeps charging, all the way
to 100 %. The cap stops that at a level the user picks (for example 90 %):

- A charge the assistant did NOT start (the charger's solar) that reaches the
  cap is paused, and the pause is HELD: the automatic un-pause (auto-resume)
  stays off while holding, or Eco-Smart would just start again.
- The hold ends, and the charger's own solar is handed back, when the car is
  unplugged, drops HYSTERESIS_PCT below the cap, or the cap is turned off. The
  next plug-in or the next sunny spell then charges from solar again.
- A charge the assistant started itself (a night top-up, a trip, a balance) is
  never touched, so a trip target above the cap still wins.

Pure, HA-free; charge_assistant.py does the I/O.
"""

from __future__ import annotations

from typing import Any

HYSTERESIS_PCT = 3.0

NONE = "none"
PAUSE = "pause"      # stop the solar charge and start holding
RELEASE = "release"  # stop holding and hand back to the charger's own solar


def cap_of(value: Any) -> float | None:
    """The configured cap, or None when off (unset, 0, 100 or more, junk)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 100 else None


def decide(*, cap: Any, soc: float | None, charging: bool, we_started: bool,
           holding: bool, plugged: bool | None) -> str:
    """What to do about the cap right now."""
    level = cap_of(cap)
    if holding:
        if level is None or plugged is False:
            return RELEASE
        if soc is not None and soc < level - HYSTERESIS_PCT:
            return RELEASE
        return NONE
    if level is None or soc is None or not charging or we_started:
        return NONE
    return PAUSE if soc >= level else NONE
