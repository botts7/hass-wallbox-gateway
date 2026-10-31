"""Legacy schedule writes for pre-6.11 Pulsar Plus firmware (issue #13).

Newer charger firmware writes schedules with ``s_sch``. Pulsar Plus firmware
before 6.11 (e.g. 6.7.41 / 6.7.43, the latest some hardware revisions get)
has no ``s_sch`` and answers ``No dispatch method found``. Those chargers take
the older ``w_sch`` with a packed string, decoded and confirmed on a 6.7.41
Plus (esp32-wallbox #50):

    <sid:2 digits><start HHMM UTC><stop HHMM UTC><days:3 digits>

``days`` is a Sunday-first bitmask (Sun=1, Mon=2, Tue=4 .. Sat=64), zero
padded. There are no enabled / current / energy-target fields. A delete is
the slot written back all-zero, which is what the gateway dashboard does.

Slots differ by model, so slot 0 is only ever written when the charger itself
reports a schedule there:
- 6.7.41 Pulsar Plus (#50): slots 1..4; r_schs never reports slot 0, and
  writing it garbled the new schedule and overwrote an existing one.
- 6.7.43 Pulsar Plus Socket (#13): r_schs reports slot 0 as a real schedule,
  and editing it maps 1:1. That unit also drops a write to an empty slot, so
  every legacy write is read back (row_applied / slot_cleared).

The original (Zentri, no-WiFi) Pulsar uses a different packed format (1-digit,
0-based slot) and doesn't answer r_schs, so it's refused here; its schedules
are edited from the gateway dashboard.

Pure helpers only; schedule.py does the I/O.
"""

from __future__ import annotations

LEGACY_SLOTS = range(0, 5)        # 0..4 can be addressed
NEW_SCHEDULE_SLOTS = range(1, 5)  # a new schedule never goes in slot 0

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
    """Packed w_sch parameter, e.g. (2, 1400, 2000, 62) -> '0214002000062'.
    Only packs; whether a slot may be written is pick_legacy_sid's call."""
    if sid not in LEGACY_SLOTS:
        raise ValueError(f"legacy schedule slot must be 0-4, got {sid}")
    if not 0 <= days <= 127:
        raise ValueError(f"days bitmask must be 0-127, got {days}")
    return f"{sid:02d}{start_utc:04d}{stop_utc:04d}{days:03d}"


def wsch_clear_par(sid: int) -> str:
    """Packed w_sch parameter that empties a slot (the legacy delete)."""
    return wsch_par(sid, 0, 0, 0)


def pick_legacy_sid(requested: int | None, existing: list[int]) -> int:
    """Slot to write on legacy firmware.

    Slot 0 only when the charger already reports a schedule there (an edit).
    Otherwise a requested slot must be 1-4, and with none requested the first
    free slot from 1 is taken. Raises ValueError for slot 0 with no schedule in
    it, an out-of-range slot, or when slots 1-4 are all in use.
    """
    if requested is not None:
        if requested == 0:
            if 0 in existing:
                return 0
            raise ValueError(
                "slot 0 can only be edited when the charger already has a "
                "schedule there (writing an empty slot 0 corrupts schedules on "
                "some Pulsar Plus firmware); use slot 1-4"
            )
        if requested not in NEW_SCHEDULE_SLOTS:
            raise ValueError(f"this charger firmware has schedule slots 0-4, got {requested}")
        return requested
    used = set(existing)
    for sid in NEW_SCHEDULE_SLOTS:
        if sid not in used:
            return sid
    raise ValueError("schedule slots 1-4 are all in use; edit or delete one first")


def check_clear_sid(sid: int, existing: list[int]) -> None:
    """A delete may target slot 0 only when the charger reports a schedule there."""
    if sid == 0 and 0 not in existing:
        raise ValueError("there is no schedule in slot 0 to delete")
    if sid not in LEGACY_SLOTS:
        raise ValueError(f"this charger firmware has schedule slots 0-4, got {sid}")


def days_mask(value: object) -> int | None:
    """A row's days as a Sunday-first bitmask, from an int or a Sun..Sat array."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return sum((1 << i) for i, on in enumerate(value[:7]) if on)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _hhmm(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def row_applied(rows: list, sid: int, start_utc: int, stop_utc: int, days: int) -> bool:
    """Did the charger store this schedule? (Read-back after a legacy write.)"""
    for r in rows or []:
        if not isinstance(r, dict) or _hhmm(r.get("sid")) != sid:
            continue
        return (_hhmm(r.get("start")) == start_utc and _hhmm(r.get("stop")) == stop_utc
                and days_mask(r.get("days")) == days)
    return False


def slot_cleared(rows: list, sid: int) -> bool:
    """After a legacy delete: the slot is gone, or left with no days."""
    for r in rows or []:
        if isinstance(r, dict) and _hhmm(r.get("sid")) == sid:
            return not days_mask(r.get("days"))
    return True


def is_original_pulsar(status: object) -> bool:
    """The original (Zentri, no-WiFi) Pulsar: Zentri radio and no project name.
    Older gateway firmware also sets ``zentri`` for a Pulsar Plus on that radio
    (#74), which reports its project, so the project is what tells them apart."""
    if not isinstance(status, dict) or not status.get("zentri"):
        return False
    return not str(status.get("chg_project") or "").strip()
