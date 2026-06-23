"""Cheapest-window charge planner — pure, testable functions.

Given a price-forecast entity (Nordpool / Amber / Tibber / generic) and a
charge requirement (energy needed, charge power, deadline), work out which
upcoming time slots are the cheapest set that delivers the energy in time,
and whether *now* falls inside one of them.

Kept free of Home Assistant imports (except dt parsing helpers passed in) so
it can be unit-tested in isolation — no hass, no entities, just data in/out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Attribute names that hold a list of forecast points, across integrations.
_LIST_ATTRS = (
    "raw_today", "raw_tomorrow",      # Nord Pool / Tibber-style
    "forecast", "forecasts",          # Amber / generic
    "prices", "today", "tomorrow",    # misc
)
# Per-point field names (first match wins).
_START_KEYS = ("start", "start_time", "startsAt", "from", "datetime", "time", "hour")
_END_KEYS = ("end", "end_time", "endsAt", "to")
_PRICE_KEYS = ("value", "per_kwh", "price", "total", "cost", "amount", "rate")


@dataclass(frozen=True)
class Slot:
    start: datetime   # tz-aware UTC
    end: datetime     # tz-aware UTC
    price: float


def _first(d: dict, keys) -> object:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def parse_forecast(attributes: dict, parse_dt, now: datetime) -> list[Slot]:
    """Flatten a price entity's attributes into sorted, future-relevant Slots.

    `parse_dt` is a callable (ISO string | datetime) -> aware datetime | None
    (pass homeassistant.util.dt.parse_datetime + as_utc, or a test stub).
    Unknown shapes yield []. Missing per-point end times are inferred from the
    next point's start (or +60 min for the last point).
    """
    if not isinstance(attributes, dict):
        return []
    raw: list[tuple[datetime, datetime | None, float]] = []
    for attr in _LIST_ATTRS:
        seq = attributes.get(attr)
        if not isinstance(seq, list):
            continue
        for pt in seq:
            if not isinstance(pt, dict):
                continue
            s = parse_dt(_first(pt, _START_KEYS))
            if s is None:
                continue
            e = parse_dt(_first(pt, _END_KEYS))
            p = _first(pt, _PRICE_KEYS)
            try:
                price = float(p)
            except (TypeError, ValueError):
                continue
            raw.append((s, e, price))

    if not raw:
        return []
    # De-dup by start time (some integrations repeat across raw_today/tomorrow).
    by_start: dict[datetime, tuple[datetime, datetime | None, float]] = {}
    for s, e, p in raw:
        by_start[s] = (s, e, p)
    pts = sorted(by_start.values(), key=lambda t: t[0])

    slots: list[Slot] = []
    for i, (s, e, p) in enumerate(pts):
        if e is None:
            e = pts[i + 1][0] if i + 1 < len(pts) else s + timedelta(minutes=60)
        if e <= s:
            continue
        slots.append(Slot(s, e, p))
    # Only keep slots that haven't fully passed.
    return [sl for sl in slots if sl.end > now]


def _clip(slot: Slot, lo: datetime, hi: datetime) -> tuple[datetime, datetime] | None:
    s = max(slot.start, lo)
    e = min(slot.end, hi)
    return (s, e) if e > s else None


def plan_cheapest(
    slots: list[Slot],
    energy_kwh: float,
    power_kw: float,
    now: datetime,
    deadline: datetime,
) -> list[Slot]:
    """Pick the cheapest set of slots between now and deadline whose combined
    (clipped) duration covers the time needed to deliver `energy_kwh` at
    `power_kw`. Returns the chosen slots (clipped to [now, deadline]) sorted by
    start. If the window can't supply enough, returns all usable slots (charge
    as much as possible — better than missing the target)."""
    if energy_kwh <= 0 or power_kw <= 0 or deadline <= now:
        return []
    needed_h = energy_kwh / power_kw
    usable: list[Slot] = []
    for sl in slots:
        c = _clip(sl, now, deadline)
        if c:
            usable.append(Slot(c[0], c[1], sl.price))
    # Cheapest first; tie-break earliest so we front-load equal-price energy.
    usable.sort(key=lambda s: (s.price, s.start))
    chosen: list[Slot] = []
    acc = 0.0
    for sl in usable:
        if acc >= needed_h:
            break
        chosen.append(sl)
        acc += (sl.end - sl.start).total_seconds() / 3600.0
    chosen.sort(key=lambda s: s.start)
    return chosen


def is_charge_now(chosen: list[Slot], now: datetime) -> bool:
    """True if `now` falls within any chosen slot."""
    return any(sl.start <= now < sl.end for sl in chosen)


def next_window(chosen: list[Slot], now: datetime) -> Slot | None:
    """The next upcoming chosen slot (for the summary / 'starts at' display)."""
    upcoming = [sl for sl in chosen if sl.end > now]
    return min(upcoming, key=lambda s: s.start) if upcoming else None
