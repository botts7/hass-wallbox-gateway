"""When is the car usually home and plugged in?

Learns, per half hour, how often the car has been plugged in at this time on
this kind of day, so a plan can count tomorrow's solar only in hours the car
is likely to be there to use it.

Data is a plain dict, so it can be saved to a Store as is:
``{"YYYY-MM-DD": [48 x (1 plugged, 0 not, None unknown)], ...}``.

- Samples come from the charger's own "car connected" (home *and* pluggable).
- The same weekday counts most; the same kind of day (weekday or weekend)
  counts too, so a pattern shows up before there are many of each weekday.
- Recent days count more (half-life ``HALF_LIFE_DAYS``).
- Too little history means "unknown" (None), and the caller assumes the car is
  away, i.e. charges overnight as before.

Pure, HA-free; charge_assistant.py does the I/O.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

SLOTS = 48                 # half hours per day
KEEP_DAYS = 42             # history kept
HALF_LIFE_DAYS = 14.0      # recency weighting
SAME_WEEKDAY_WEIGHT = 2.0  # a Saturday says more about Saturdays than a Sunday does


def slot_of(t: datetime) -> int:
    """Half-hour slot (0-47) of a local time."""
    return t.hour * 2 + (1 if t.minute >= 30 else 0)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def record(days: dict, t: datetime, plugged: bool) -> None:
    """Note whether the car is plugged in at local time ``t``. A slot that
    sees "plugged" at any sample stays plugged for that slot."""
    key = t.date().isoformat()
    row = days.get(key)
    if not isinstance(row, list) or len(row) != SLOTS:
        row = [None] * SLOTS
        days[key] = row
    s = slot_of(t)
    row[s] = 1 if (plugged or row[s] == 1) else 0


def prune(days: dict, today: date, keep: int = KEEP_DAYS) -> None:
    """Drop days older than ``keep`` and anything unparseable."""
    for k in list(days):
        try:
            d = date.fromisoformat(k)
        except (TypeError, ValueError):
            del days[k]
            continue
        if (today - d).days > keep or d > today:
            del days[k]


def history_days(days: dict, weekend: bool | None = None) -> int:
    """Days with any data, optionally only weekends or only weekdays."""
    n = 0
    for k, row in days.items():
        try:
            d = date.fromisoformat(k)
        except (TypeError, ValueError):
            continue
        if weekend is not None and is_weekend(d) != weekend:
            continue
        if isinstance(row, list) and any(v is not None for v in row):
            n += 1
    return n


def probability(days: dict, target: datetime, today: date,
                min_days: int = 14) -> float | None:
    """Chance (0-1) the car is home and plugged in at local time ``target``,
    or None while there isn't enough history of that kind of day."""
    weekend = is_weekend(target.date())
    # Enough history means enough days of this KIND (weekday / weekend):
    # weekends are scarcer, so they need proportionally fewer.
    need = max(1, round(min_days * (2 / 7))) if weekend else max(1, round(min_days * (5 / 7)))
    if history_days(days, weekend) < need:
        return None
    s = slot_of(target)
    num = den = 0.0
    for k, row in days.items():
        try:
            d = date.fromisoformat(k)
        except (TypeError, ValueError):
            continue
        if is_weekend(d) != weekend or not isinstance(row, list) or len(row) != SLOTS:
            continue
        v = row[s]
        if v is None:
            continue
        age = max(0, (today - d).days)
        w = 0.5 ** (age / HALF_LIFE_DAYS)
        if d.weekday() == target.weekday():
            w *= SAME_WEEKDAY_WEIGHT
        num += w * v
        den += w
    if den == 0:
        return None
    return num / den


def set_slot(days: dict, slot_start: datetime, home: bool) -> None:
    """Record a finished half hour: home only when the car was plugged in for
    all of it (arriving or leaving mid-slot counts as away)."""
    key = slot_start.date().isoformat()
    row = days.get(key)
    if not isinstance(row, list) or len(row) != SLOTS:
        row = [None] * SLOTS
        days[key] = row
    row[slot_of(slot_start)] = 1 if home else 0


def seed_from_intervals(days: dict, changes: list, start: datetime, end: datetime,
                        overwrite: bool = False) -> int:
    """Fill ``days`` from a state history: ``changes`` is a time-ordered list of
    (local datetime, plugged bool or None). A half-hour slot between ``start``
    and ``end`` is home only when the car was plugged in at its start and never
    unplugged during it; unknown states (e.g. a restart blip) are ignored.
    Slots with no known state yet are skipped. Existing values are kept unless
    ``overwrite``. Returns how many slots were filled."""
    if not changes:
        return 0
    filled = 0
    i = 0
    state = None
    t = start.replace(minute=(0 if start.minute < 30 else 30), second=0, microsecond=0)
    while t < end:
        while i < len(changes) and changes[i][0] <= t:
            if changes[i][1] is not None:
                state = changes[i][1]
            i += 1
        at_start = state
        home = at_start is True
        slot_end = t + timedelta(minutes=30)
        j = i
        while j < len(changes) and changes[j][0] < slot_end:
            if changes[j][1] is False:
                home = False
            j += 1
        if at_start is not None and slot_end <= end:
            key = t.date().isoformat()
            row = days.get(key)
            if not isinstance(row, list) or len(row) != SLOTS:
                row = [None] * SLOTS
                days[key] = row
            s = slot_of(t)
            if row[s] is None or overwrite:
                row[s] = 1 if home else 0
                filled += 1
        t = slot_end
    return filled
