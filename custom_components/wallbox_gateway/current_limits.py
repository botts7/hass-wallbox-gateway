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


def ceiling_from(*sources: Mapping[str, Any] | None) -> int:
    """First valid max_available_current among ``sources``, else 32 A."""
    for src in sources:
        if not isinstance(src, Mapping):
            continue
        v = src.get("max_available_current")
        if v is None:
            v = src.get("maxAvailableCurrent")
        if isinstance(v, bool) or not isinstance(v, int):
            continue
        if MIN_CURRENT_A <= v <= ABSOLUTE_MAX_CURRENT_A:
            return v
    return DEFAULT_MAX_CURRENT_A


def current_ceiling(data: Mapping[str, Any] | None) -> int:
    """Ceiling from a coordinator's ``data`` dict: realtime first, then status,
    then the gateway's saved ceiling (``max_current_ceiling`` in /api/status,
    newer firmware), which is known from boot, before the first charger poll."""
    if not isinstance(data, Mapping):
        return DEFAULT_MAX_CURRENT_A
    c = ceiling_from(data.get("charger_realtime"), data.get("charger_status"))
    if c != DEFAULT_MAX_CURRENT_A:
        return c
    raw = data.get("raw_status")
    saved = raw.get("max_current_ceiling") if isinstance(raw, Mapping) else None
    if isinstance(saved, int) and not isinstance(saved, bool) and             MIN_CURRENT_A <= saved <= ABSOLUTE_MAX_CURRENT_A:
        return saved
    return c


def user_max(stored: Any, ceiling: int) -> int:
    """The Charge Assistant's own maximum current: what the user saved, within
    the ceiling; unset means the full ceiling. A saved 32 on a charger with a
    higher ceiling is the old default (forms saved 32 before #39, or when the
    ceiling hadn't loaded), so it counts as unset."""
    try:
        v = int(stored)
    except (TypeError, ValueError):
        return ceiling
    if v <= 0:
        return ceiling
    if v == DEFAULT_MAX_CURRENT_A and ceiling > DEFAULT_MAX_CURRENT_A:
        return ceiling
    return max(MIN_CURRENT_A, min(v, ceiling))


def clamp_current(amps: float, ceiling: int) -> int:
    """Clamp to MIN_CURRENT_A..ceiling."""
    return int(max(MIN_CURRENT_A, min(int(amps), ceiling)))
