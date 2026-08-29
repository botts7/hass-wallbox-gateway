"""Next scheduled charge in the charger's local timezone.

The Wallbox charger stores each schedule's ``days`` as a LOCAL weekday bitmask
(bit0=Sun) but ``start`` as a UTC ``HHMM``. The gateway firmware reports
``next_scheduled_charge`` computed in UTC, which lands a local-midnight window on
the wrong day (a Sydney "Sunday 00:00" == 14:00 UTC reads as Monday). We
recompute it here with a real timezone database (:mod:`zoneinfo`), anchoring on
the stored UTC start instant and checking the LOCAL weekday of that instant —
DST-correct, no offset arithmetic, nothing hardcoded (the offset comes from the
tz database seeded by the charger's own ``g_tzn`` zone name).

Mirrors ``app/next_charge.py`` in the wallbox-gateway-ha-addon repo.
"""

from __future__ import annotations

from datetime import datetime

try:  # stdlib on 3.9+; Home Assistant ships tzdata
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _hhmm_to_sec(v) -> int:
    """"1400" / 1400 (UTC HHMM) -> seconds since UTC midnight."""
    try:
        n = int(str(v))
    except (TypeError, ValueError):
        return 0
    return (n // 100) * 3600 + (n % 100) * 60


def compute_next_charge(schedules, tz_name, now_epoch) -> int | None:
    """Earliest future charge-start (UTC epoch) across all ENABLED schedules, or
    ``None`` if there are none / the timezone is unknown.

    ``schedules`` is the raw ``r_schs`` list ([{days, start, enabled, ...}]).
    """
    if not schedules or ZoneInfo is None or not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - unknown zone -> no computed value
        return None

    now_epoch = int(now_epoch)
    utc_midnight = (now_epoch // 86400) * 86400
    best: int | None = None
    for s in schedules:
        if not isinstance(s, dict) or not s.get("enabled"):
            continue
        try:
            days = int(s.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        if not days:
            continue
        start_sec = _hhmm_to_sec(s.get("start"))
        # Walk the next 8 UTC days; the stored start is a fixed UTC instant, so
        # convert each candidate to local time and match the LOCAL weekday bit.
        for k in range(8):
            inst = utc_midnight + k * 86400 + start_sec
            if inst <= now_epoch:
                continue
            wday = (datetime.fromtimestamp(inst, tz).weekday() + 1) % 7  # Sun=0
            if (days >> wday) & 1:
                if best is None or inst < best:
                    best = inst
                break  # earliest occurrence for this schedule
    return best
