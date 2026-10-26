"""Legacy schedule writes for pre-6.11 Pulsar Plus firmware (issue #13).

Newer charger firmware writes schedules with ``s_sch``. Pulsar Plus firmware
before 6.11 (e.g. 6.7.41 / 6.7.43, the latest some hardware revisions get)
has no ``s_sch`` and answers ``No dispatch method found``. Those chargers take
the older ``w_sch`` with a packed string, decoded and confirmed on a 6.7.41
Plus (esp32-wallbox #50):

    <sid:2 digits><start HHMM UTC><stop HHMM UTC><days:3 digits>

``days`` is a Sunday-first bitmask (Sun=1, Mon=2, Tue=4 .. Sat=64), zero
padded. There are no enabled / current / energy-target fields. Slots are
1-based (1..4): writing slot 0 on 6.7.41 garbled the new schedule and
overwrote an existing one, so slot 0 is never written here. A delete is the
slot written back all-zero, which is what the gateway dashboard does.

Pure helpers only; schedule.py does the I/O.
"""

from __future__ import annotations

LEGACY_SLOTS = range(1, 5)  # 1..4

_DAY_BITS = {"sun": 1, "mon": 2, "tue": 4, "wed": 8, "thu": 16, "fri": 32, "sat": 64}


def is_no_dispatch(res: object) -> bool:
    """True when the charger says the method doesn't exist on this firmware."""
    if not isinstance(res, dict):
        return False
    err = res.get("error")
    msg = err.get("message") if isinstance(err, dict) else err
    return isinstance(msg, str) and "no dispatch method" in msg.lower()


def days_bitmask(days: list[str]) -> int:
    """['sun', 'fri'] -> 33. Sunday-first, matching what the charger stores."""
    return sum(_DAY_BITS[d.lower()] for d in {d.lower() for d in days} if d in _DAY_BITS)


def wsch_par(sid: int, start_utc: int, stop_utc: int, days: int) -> str:
    """Packed w_sch parameter, e.g. (2, 1400, 2000, 62) -> '0214002000062'."""
    if sid not in LEGACY_SLOTS:
        raise ValueError(f"legacy schedule slot must be 1-4, got {sid}")
    if not 0 <= days <= 127:
        raise ValueError(f"days bitmask must be 0-127, got {days}")
    return f"{sid:02d}{start_utc:04d}{stop_utc:04d}{days:03d}"


def wsch_clear_par(sid: int) -> str:
    """Packed w_sch parameter that empties a slot (the legacy delete)."""
    return wsch_par(sid, 0, 0, 0)


def pick_legacy_sid(requested: int | None, existing: list[int]) -> int:
    """Slot to write on legacy firmware.

    A requested slot is used as-is if it's 1-4. With none requested, the first
    free slot from 1 is taken. Raises ValueError for slot 0 (unsafe on this
    firmware), an out-of-range slot, or when all four are in use.
    """
    if requested is not None:
        if requested == 0:
            raise ValueError(
                "slot 0 can't be written on this charger firmware (it corrupts "
                "schedules on pre-6.11 Pulsar Plus); use slot 1-4"
            )
        if requested not in LEGACY_SLOTS:
            raise ValueError(f"this charger firmware has schedule slots 1-4, got {requested}")
        return requested
    used = set(existing)
    for sid in LEGACY_SLOTS:
        if sid not in used:
            return sid
    raise ValueError("all 4 schedule slots are in use; edit or delete one first")
