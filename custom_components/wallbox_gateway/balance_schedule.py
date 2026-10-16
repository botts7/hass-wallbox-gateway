"""Periodic battery-balance schedule — pure, testable.

LFP (and to a lesser extent NMC) packs benefit from a periodic full charge so
the BMS can rebalance cells and recalibrate its SOC estimate. Rather than
charge to 100% every night (bad for calendar ageing, and often needless), the
Charge Assistant charges only to the everyday floor on normal nights and lifts
that to a balance target (default 100%) once every N days.

This module owns just the "is a balance due, and when's the next one" decision
and the resulting night target. It is deliberately free of Home Assistant
imports so it can be unit-tested with plain ``date`` objects — the controller
passes in the local date, reads/writes the persisted last-balance date, and
does the actual charging.

Dates are local calendar dates (a balance "night" belongs to the date the
window opens). ``last_balance`` is the date the pack last reached the balance
target, or ``None`` if it never has under this config.
"""

from __future__ import annotations

from datetime import date, timedelta

# Sensible bounds so a typo in config can't disable balancing or hammer the
# pack. Weekly is the common LFP recommendation; fortnightly is the gentlest
# we offer.
MIN_INTERVAL_DAYS = 1
MAX_INTERVAL_DAYS = 90
DEFAULT_INTERVAL_DAYS = 7
DEFAULT_BALANCE_TARGET_PCT = 100.0


def _parse_date(value) -> date | None:
    """Accept a ``date``, a datetime, or an ISO 'YYYY-MM-DD' string → date."""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        # datetime is a subclass of date; normalise to the date part.
        return date(value.year, value.month, value.day)
    try:
        y, m, d = str(value).strip()[:10].split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def clamp_interval(days) -> int:
    """Coerce a configured interval to a sane int in [MIN, MAX]."""
    try:
        n = int(days)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_DAYS
    return max(MIN_INTERVAL_DAYS, min(n, MAX_INTERVAL_DAYS))


def is_balance_due(last_balance, today, interval_days) -> bool:
    """Is a balance charge due on/by ``today``?

    Due when the pack has never been balanced under this config, or when at
    least ``interval_days`` have elapsed since the last balance. A future
    ``last_balance`` (clock skew / manual edit) is treated as not due.
    """
    last = _parse_date(last_balance)
    day = _parse_date(today)
    if day is None:
        return False
    if last is None:
        return True
    if last > day:
        return False
    return (day - last).days >= clamp_interval(interval_days)


def next_balance_date(last_balance, today, interval_days) -> date:
    """The next calendar date a balance is due (today if already due/overdue)."""
    last = _parse_date(last_balance)
    day = _parse_date(today) or date(1970, 1, 1)
    if last is None or last > day:
        return day
    due = last + timedelta(days=clamp_interval(interval_days))
    return due if due > day else day


def night_target_pct(
    floor_pct: float,
    *,
    balance_enabled: bool,
    balance_due: bool,
    balance_target_pct: float = DEFAULT_BALANCE_TARGET_PCT,
) -> tuple[float, bool]:
    """The SOC target for tonight and whether it's a balance night.

    Normal nights target the everyday floor. When balancing is enabled and due,
    tonight targets ``balance_target_pct`` instead (never *below* the floor — a
    misconfigured low balance target still charges at least the floor).
    """
    if balance_enabled and balance_due:
        return max(float(floor_pct), float(balance_target_pct)), True
    return float(floor_pct), False
