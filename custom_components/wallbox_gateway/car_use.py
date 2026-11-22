"""Will the car be needed before the cheap window? (for 'wait for cheap hours')

A critical or floor top-up outside the cheap window can wait for the window,
unless the car is likely to leave first. That is decided from, in order:

- departures: a known departure time (the daily departure setting, or an
  optional departure entity: a timestamp sensor, an input_datetime, or a
  calendar's next timed event, optionally matching a keyword) between now
  and the window start means needed;
- the car's usual pattern for each half hour from now to the window start
  (learned from an optional car location entity, else from the charger's
  plug-in history): a half hour the car is usually not home means needed;
- no pattern yet (not enough history): needed, so it never leaves you short.

Pure, HA-free; charge_assistant.py and solar_planner.py do the I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

_SLOT = timedelta(minutes=30)
_HOME_STATES = {"home", "on", "true"}
_UNKNOWN = {"unknown", "unavailable", "", "none"}


def is_home(domain: str, state: str | None) -> bool | None:
    """Whether an entity's state says the car is home. device_tracker / person:
    'home' (any zone or not_home is away); binary_sensor / input_boolean: on.
    None while unknown."""
    s = str(state or "").strip().lower()
    if s in _UNKNOWN:
        return None
    if domain in ("binary_sensor", "input_boolean", "switch"):
        return s in ("on", "true")
    return s in _HOME_STATES


def _matches(ev: dict, keyword: str | None) -> bool:
    if not keyword:
        return True
    k = keyword.strip().lower()
    hay = " ".join(str(ev.get(f) or "") for f in ("summary", "description", "location")).lower()
    return k in hay


def next_departure(events, now: datetime, keyword: str | None = None) -> datetime | None:
    """Earliest timed event after ``now`` (matching ``keyword`` in its summary,
    description or location, if given), as an aware datetime. All-day events
    (a date with no time) are never departures. Events are calendar.get_events
    dicts: {"start": iso, "summary", "description", "location"}."""
    best = None
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        start = ev.get("start")
        if not isinstance(start, str) or "T" not in start:
            continue   # missing, or an all-day event
        try:
            d = datetime.fromisoformat(start)
        except ValueError:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=now.tzinfo)
        if d <= now or not _matches(ev, keyword):
            continue
        if best is None or d < best:
            best = d
    return best


def event_from_attributes(attrs: dict, tz) -> dict | None:
    """A calendar entity's own attributes (its current or next event) as a
    get_events-style dict, or None for an all-day or missing event."""
    if not isinstance(attrs, dict) or attrs.get("all_day") or not attrs.get("start_time"):
        return None
    try:
        d = datetime.strptime(str(attrs["start_time"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return None
    return {"start": d.isoformat(), "summary": attrs.get("message") or "",
            "description": attrs.get("description") or "",
            "location": attrs.get("location") or ""}


def needed_before(now: datetime, until: datetime, chance: Callable[[datetime], float | None],
                  threshold: float, departures: list | None = None) -> bool:
    """True when the car is likely to be needed (away) between ``now`` and
    ``until``. ``chance(t)`` is the chance the car is home in the half hour
    starting at ``t`` (None = not enough history)."""
    if until <= now:
        return False
    for d in departures or []:
        if d is not None and now < d <= until:
            return True
    t = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    while t < until:
        p = chance(t)
        if p is None or p < threshold:
            return True
        t += _SLOT
    return False
