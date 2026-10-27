"""Schedule format of the original (Zentri, no-WiFi) Pulsar.

Decoded from an HCI snoop of the Wallbox app on an original Pulsar (fw_v_ 375,
esp32-wallbox #68):

- four fixed slots, 0..3, read one at a time with ``r_sch <sid>`` (this module
  doesn't answer r_schs). An empty slot reads back as all zeros:
  ``{"sid":1,"start":"0000","stop":"0000","days":0,"mcr":0,"nrg":0}``.
- write: ``w_sch "<sid:1><start HHMM UTC><stop HHMM UTC><days:3>"``, 12
  characters, e.g. ``023000500127`` = slot 0, 23:00 to 05:00 UTC, every day
  (Sunday-first bitmask). Writing into an empty slot creates the schedule.
- delete: the slot written back all zero, e.g. ``000000000000``.
- no per-schedule current or energy limit: mcr / nrg are always 0, and the
  app's schedule-screen current is the charger-wide ``w_mxI``.

The Pulsar Plus legacy format (2-digit slot, r_schs) is in legacy_schedule.py.
Pure helpers only; schedule.py does the I/O.
"""

from __future__ import annotations

SLOTS = range(0, 4)


def wsch_par(sid: int, start_utc: int, stop_utc: int, days: int) -> str:
    """Packed w_sch for the original Pulsar, e.g. (0, 2300, 500, 127) -> '023000500127'."""
    if sid not in SLOTS:
        raise ValueError(f"the original Pulsar has schedule slots 0-3, got {sid}")
    if not 0 <= days <= 127:
        raise ValueError(f"days bitmask must be 0-127, got {days}")
    return f"{sid:d}{start_utc:04d}{stop_utc:04d}{days:03d}"


def clear_par(sid: int) -> str:
    """Packed w_sch that empties a slot (the app's delete)."""
    return wsch_par(sid, 0, 0, 0)


def _int(v: object) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def in_use(row: object) -> bool:
    """A slot holds a schedule when it has days set."""
    return isinstance(row, dict) and bool(_int(row.get("days")))


def used_slots(rows: list) -> list[int]:
    return [_int(r.get("sid")) for r in rows or [] if in_use(r) and _int(r.get("sid")) in SLOTS]


def pick_sid(requested: int | None, rows: list) -> int:
    """Slot to write: the requested one (0-3), else the first empty slot."""
    if requested is not None:
        if requested not in SLOTS:
            raise ValueError(f"the original Pulsar has schedule slots 0-3, got {requested}")
        return requested
    used = set(used_slots(rows))
    for sid in SLOTS:
        if sid not in used:
            return sid
    raise ValueError("all 4 schedule slots (0-3) are in use; edit or delete one first")


def row_applied(rows: list, sid: int, start_utc: int, stop_utc: int, days: int) -> bool:
    for r in rows or []:
        if isinstance(r, dict) and _int(r.get("sid")) == sid:
            return (_int(r.get("start")) == start_utc and _int(r.get("stop")) == stop_utc
                    and _int(r.get("days")) == days)
    return False


def slot_cleared(rows: list, sid: int) -> bool:
    for r in rows or []:
        if isinstance(r, dict) and _int(r.get("sid")) == sid:
            return not in_use(r)
    return False   # an unreadable slot isn't proof it was cleared
