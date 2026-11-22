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


def next_departure(events, now: datetime, keyword: str | None = None,
                   tz=None) -> datetime | None:
    """Earliest timed event after ``now`` (matching ``keyword`` in its summary,
    description or location, if given), as an aware datetime. All-day events
    (a date with no time) are never departures, nor are events located at
    "home". Times without a zone are in ``tz`` (HA's time zone; defaults to
    ``now``'s). Events are calendar.get_events dicts:
    {"start": iso, "summary", "description", "location"}."""
    zone = tz or now.tzinfo
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
            d = d.replace(tzinfo=zone)
        if d <= now or not _matches(ev, keyword):
            continue
        if str(ev.get("location") or "").strip().lower() == "home":
            continue   # an event at home isn't a drive
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


def departure_from_state(domain: str, state, attrs: dict | None, now: datetime,
                         tz) -> datetime | None:
    """A departure time from a timestamp sensor or an input_datetime state, as
    an aware datetime in ``tz`` (HA's time zone), or None.

    - sensor: an ISO timestamp (a zone-less one is read in ``tz``);
    - input_datetime with date and time: "YYYY-MM-DD HH:MM:SS" in ``tz``;
    - input_datetime with a time only: the next time it comes round;
    - input_datetime with a date only: no time, so no departure."""
    s = str(state or "").strip()
    if s.lower() in _UNKNOWN:
        return None
    attrs = attrs if isinstance(attrs, dict) else {}
    if domain == "input_datetime":
        has_date = attrs.get("has_date", True)
        has_time = attrs.get("has_time", True)
        if not has_time:
            return None
        if not has_date:
            try:
                t = datetime.strptime(s, "%H:%M:%S").time()
            except ValueError:
                return None
            local = now.astimezone(tz)
            d = datetime.combine(local.date(), t, tzinfo=tz)
            if d <= local:
                d = datetime.combine(local.date() + timedelta(days=1), t, tzinfo=tz)
            return d
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        except ValueError:
            return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=tz)


def needed_reason(now: datetime, until: datetime, chance: Callable[[datetime], float | None],
                  threshold: float, departures: list | None = None) -> str | None:
    """Why the car is likely to be needed (away) between ``now`` and ``until``,
    or None when it isn't. ``chance(t)`` is the chance the car is home in the
    half hour starting at ``t`` (None = not enough history)."""
    if until <= now:
        return None
    for d in departures or []:
        if d is not None and now < d <= until:
            return f"departure at {d.strftime('%H:%M')}"
    t = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    while t < until:
        p = chance(t)
        if p is None:
            return f"not enough history for {t.strftime('%a %H:%M')}"
        if p < threshold:
            return f"usually out at {t.strftime('%a %H:%M')} (home {p:.0%})"
        t += _SLOT
    return None


def needed_before(now: datetime, until: datetime, chance: Callable[[datetime], float | None],
                  threshold: float, departures: list | None = None) -> bool:
    """True when the car is likely to be needed before ``until`` (needed_reason)."""
    return needed_reason(now, until, chance, threshold, departures) is not None
