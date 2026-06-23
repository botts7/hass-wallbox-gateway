"""Charge guards — battery care + cost cap (Phase 4). Pure, testable.

Kept free of Home Assistant imports so the decisions are unit-tested in
isolation.

* effective_target — the SOC ceiling to use *now*. A daily "battery care"
  target (e.g. 80 %) protects the pack; an optional higher trip target applies
  only until a deadline (e.g. 100 % before Friday's trip) and then auto-reverts
  by time — no one-shot flag to persist or reset.

* price_allows_charge — a hard cost ceiling: never charge above a price cap.
  Composes with cheapest-window; a caller's departure floor overrides it so the
  car is still ready in time. Unknown/invalid price fails OPEN (doesn't block) —
  the departure floor remains the real safety net.
"""

from __future__ import annotations


def effective_target(daily_target, trip_target, trip_until, now):
    """Resolve the active SOC target. `trip_until`/`now` are tz-aware datetimes
    (or None). The trip target only wins while now < trip_until, and only if it
    is actually higher than the daily target."""
    try:
        daily = float(daily_target)
    except (TypeError, ValueError):
        daily = 80.0
    if trip_target and trip_until is not None and now is not None and now < trip_until:
        try:
            return max(daily, float(trip_target))
        except (TypeError, ValueError):
            return daily
    return daily


def derive_surplus(source, surplus=None, grid=None, solar=None, load=None,
                   grid_export_negative=True):
    """Compute available solar surplus from whatever sensors the user has.

      source='entity'      → use a ready-made surplus sensor directly
      source='grid'        → export power from a grid sensor (negative = export
                             by default, so surplus = max(0, -grid))
      source='solar_load'  → solar production minus house load

    Values are in the source sensors' own units (keep them consistent). Returns
    None when the needed inputs are missing/non-numeric (caller then does
    nothing — fail-safe)."""
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    if source == "grid":
        g = _num(grid)
        if g is None:
            return None
        return max(0.0, -g if grid_export_negative else g)
    if source == "solar_load":
        s, l = _num(solar), _num(load)
        if s is None or l is None:
            return None
        return max(0.0, s - l)
    # default: a direct surplus sensor
    return _num(surplus)


def price_allows_charge(price, cap):
    """True if charging is allowed under the price cap. No cap → always allowed.
    Unknown price → allowed (fail-open; the departure floor still protects)."""
    if cap in (None, ""):
        return True
    try:
        cap_v = float(cap)
    except (TypeError, ValueError):
        return True
    try:
        return float(price) <= cap_v
    except (TypeError, ValueError):
        return True
