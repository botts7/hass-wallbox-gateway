"""The charger's real current ceiling, in one place.

Wallbox chargers report their own hardware / installation limit as
``max_available_current``: 32 A on most EU units, 40 A or 48 A on US Pulsar
Plus units, lower when the installer limited the circuit. A fixed 32 A cap
wrongly held 40 A chargers back (#39), first fixed only for the max-current
slider. Everything that sets or validates a charge current now asks here.

The value can arrive in r_sta (realtime) or r_dat (status) depending on the
charger firmware, so both are checked. When neither carries it, the old 32 A
default applies.

HA-free so it can be unit tested directly.
"""

from __future__ import annotations

from typing import Any, Mapping

MIN_CURRENT_A = 6
DEFAULT_MAX_CURRENT_A = 32   # when the charger doesn't report a ceiling
ABSOLUTE_MAX_CURRENT_A = 80  # sanity bound for a reported ceiling


def _valid(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if MIN_CURRENT_A <= v <= ABSOLUTE_MAX_CURRENT_A else None


def _reported(*sources: Mapping[str, Any] | None) -> int | None:
    """First valid max_available_current among ``sources``, else None."""
    for src in sources:
        if not isinstance(src, Mapping):
            continue
        v = src.get("max_available_current")
        if v is None:
            v = src.get("maxAvailableCurrent")
        if _valid(v) is not None:
            return v
    return None


def ceiling_from(*sources: Mapping[str, Any] | None) -> int:
    """First valid max_available_current among ``sources``, else 32 A."""
    v = _reported(*sources)
    return DEFAULT_MAX_CURRENT_A if v is None else v


def known_ceiling(data: Mapping[str, Any] | None) -> int | None:
    """The ceiling when the charger or gateway has actually reported one:
    realtime first, then status, then the gateway's saved ceiling
    (``max_current_ceiling`` in /api/status, newer firmware), which is known
    from boot, before the first charger poll. None when nothing has."""
    if not isinstance(data, Mapping):
        return None
    v = _reported(data.get("charger_realtime"), data.get("charger_status"))
    if v is not None:
        return v
    raw = data.get("raw_status")
    return _valid(raw.get("max_current_ceiling")) if isinstance(raw, Mapping) else None


def current_ceiling(data: Mapping[str, Any] | None) -> int:
    """Ceiling from a coordinator's ``data`` dict (see known_ceiling), else 32 A."""
    v = known_ceiling(data)
    return DEFAULT_MAX_CURRENT_A if v is None else v


def user_max(stored: Any, ceiling: int) -> int:
    """The Charge Assistant's own maximum current: what the user saved, within
    the ceiling; unset (or not a number) means the full ceiling. A saved value
    is always kept, even 32 on a 48 A charger: it can't be told apart from a
    deliberate limit, so the user is told instead (see below_ceiling)."""
    try:
        v = int(stored)
    except (TypeError, ValueError):
        return ceiling
    if v <= 0:
        return ceiling
    return max(MIN_CURRENT_A, min(v, ceiling))


def below_ceiling(stored: Any, ceiling: int) -> int | None:
    """The saved maximum when it is set and below the charger's ceiling (worth
    telling the user: older settings forms saved 32 A on their own, which
    holds a 40/48 A charger back), else None."""
    try:
        v = int(stored)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < ceiling else None


def clamp_current(amps: float, ceiling: int) -> int:
    """Clamp to MIN_CURRENT_A..ceiling."""
    return int(max(MIN_CURRENT_A, min(int(amps), ceiling)))
