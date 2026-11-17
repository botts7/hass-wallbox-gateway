"""A manual "when is the car home" schedule for the solar planner.

Optional and empty by default. When set it replaces the learned home pattern:
hours inside it count as home, every other hour as away, with no history
needed. Written as one line, entries separated by ";" or new lines:

    Mon-Fri 17:00-08:00; Sat,Sun 00:00-24:00
    Wed 09:00-15:00, 18:00-24:00

Days: Mon..Sun, ranges like Mon-Fri or Fri-Mon, lists like Sat,Sun, or
"daily". Times are HH:MM; an end earlier than the start runs past midnight
into the next day, and 24:00 means the end of the day.

Pure, HA-free; solar_planner.py does the I/O.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_RANGE = re.compile(r"(\d{1,2}):?(\d{2})?\s*-\s*(\d{1,2}):?(\d{2})?")


class ScheduleError(ValueError):
    """The text could not be read; the message says which part."""


def _day(tok: str) -> int:
    t = tok.strip().lower()[:3]
    if t not in _DAYS:
        raise ScheduleError(f"unknown day '{tok.strip()}'")
    return _DAYS.index(t)


def _days(spec: str) -> list[int]:
    spec = spec.strip()
    if spec.lower() in ("daily", "every day", "all", "*"):
        return list(range(7))
    out: list[int] = []
    for part in re.split(r"[,\s]+", spec):
        if not part:
            continue
        if "-" in part:
            a, b = (_day(x) for x in part.split("-", 1))
            d = a
            while True:
                out.append(d)
                if d == b:
                    break
                d = (d + 1) % 7
        else:
            out.append(_day(part))
    if not out:
        raise ScheduleError("missing days")
    return sorted(set(out))


def _mins(h: str, m: str | None) -> int:
    v = int(h) * 60 + int(m or 0)
    if not 0 <= v <= 24 * 60 or int(m or 0) > 59:
        raise ScheduleError(f"bad time {h}:{m or '00'}")
    return v


def parse(text: str | None) -> list[tuple[int, int, int]]:
    """``[(weekday, start_min, end_min)]`` with end > start, overnight ranges
    split at midnight. Empty text gives []."""
    out: list[tuple[int, int, int]] = []
    for entry in re.split(r"[;\n]+", text or ""):
        entry = entry.strip()
        if not entry:
            continue
        m = re.search(r"\d", entry)
        if m is None:
            raise ScheduleError(f"no times in '{entry}'")
        days = _days(entry[:m.start()])
        ranges = _RANGE.findall(entry[m.start():])
        if not ranges:
            raise ScheduleError(f"no times in '{entry}'")
        for h1, m1, h2, m2 in ranges:
            a, b = _mins(h1, m1), _mins(h2, m2)
            if a == b:
                continue
            for d in days:
                if b > a:
                    out.append((d, a, b))
                else:   # past midnight
                    out.append((d, a, 24 * 60))
                    if b > 0:
                        out.append(((d + 1) % 7, 0, b))
    return out


def is_home(sched: list, when: datetime) -> bool:
    """Whether local time ``when`` is inside the schedule."""
    d, t = when.weekday(), when.hour * 60 + when.minute
    return any(d == sd and a <= t < b for sd, a, b in sched)


def hour_chance(sched: list, hour: datetime) -> float:
    """Share of the hour starting at ``hour`` inside the schedule, in halves
    (0, 0.5 or 1), matching the learned pattern's half-hour slots."""
    a = is_home(sched, hour)
    b = is_home(sched, hour + timedelta(minutes=30))
    return (a + b) / 2
