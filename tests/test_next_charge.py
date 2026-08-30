"""Unit tests for the charger-local next-charge computation (pure, no HA).

Verifies the fix for the UTC-vs-local schedule-day bug: the charger stores
`days` as a LOCAL weekday bitmask (bit0=Sun) but `start` as a UTC HHMM, so a
Sydney "Sunday 00:00" schedule (14:00 UTC) must resolve to Sunday, not Monday.
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import next_charge as nc  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn


SYD = "Australia/Sydney"


def _epoch(y, mo, d, h, mn, tz=SYD):
    return int(datetime(y, mo, d, h, mn, tzinfo=ZoneInfo(tz)).timestamp())


def _local(ep):
    return datetime.fromtimestamp(ep, ZoneInfo(SYD))


# ── the reported bug: Sydney "Sunday 00:00" (14:00 UTC, days bit0=Sun) ───────
@case
def test_sunday_local_midnight_not_monday():
    # Sat 2026-08-29 21:30 local -> next charge is Sun 00:00 local (that night),
    # NOT Monday. Schedule stored as days=1 (Sun) + start "1400" (UTC).
    now = _epoch(2026, 8, 29, 21, 30)
    scheds = [{"days": 1, "start": "1400", "enabled": 1}]
    res = nc.compute_next_charge(scheds, SYD, now)
    loc = _local(res)
    assert loc.weekday() == 6, loc          # Python: Sunday == 6
    assert (loc.hour, loc.minute) == (0, 0), loc
    assert loc.day == 30, loc               # 2026-08-30


@case
def test_weekday_set_maps_mon_fri():
    # days=62 = bits 1..5 (Mon..Fri local). From a Sunday, the next occurrence
    # is Monday 00:00 local.
    now = _epoch(2026, 8, 30, 12, 0)        # Sunday midday
    scheds = [{"days": 62, "start": "1400", "enabled": 1}]
    loc = _local(nc.compute_next_charge(scheds, SYD, now))
    assert loc.weekday() == 0, loc          # Monday
    assert (loc.hour, loc.minute) == (0, 0), loc


@case
def test_earliest_across_schedules():
    now = _epoch(2026, 8, 29, 21, 30)       # Sat night
    scheds = [
        {"days": 62, "start": "1400", "enabled": 1},   # Mon-Fri -> Mon 00:00
        {"days": 1, "start": "1400", "enabled": 1},    # Sun     -> Sun 00:00 (sooner)
    ]
    loc = _local(nc.compute_next_charge(scheds, SYD, now))
    assert loc.weekday() == 6, loc          # Sunday wins (earlier)


@case
def test_disabled_and_empty_ignored():
    now = _epoch(2026, 8, 29, 21, 30)
    assert nc.compute_next_charge(
        [{"days": 1, "start": "1400", "enabled": 0}], SYD, now) is None
    assert nc.compute_next_charge(
        [{"days": 0, "start": "1400", "enabled": 1}], SYD, now) is None
    assert nc.compute_next_charge([], SYD, now) is None


@case
def test_unknown_or_missing_timezone():
    now = _epoch(2026, 8, 29, 21, 30)
    scheds = [{"days": 1, "start": "1400", "enabled": 1}]
    assert nc.compute_next_charge(scheds, "Not/AZone", now) is None
    assert nc.compute_next_charge(scheds, None, now) is None
    assert nc.compute_next_charge(scheds, "", now) is None


@case
def test_utc_charger_is_identity():
    # On a UTC charger, day bit and start are already the same frame.
    now = _epoch(2026, 8, 29, 21, 30, tz="UTC")
    scheds = [{"days": 1, "start": "0000", "enabled": 1}]   # Sun 00:00 UTC
    loc = datetime.fromtimestamp(
        nc.compute_next_charge(scheds, "UTC", now), ZoneInfo("UTC"))
    assert loc.weekday() == 6 and loc.hour == 0, loc


@case
def test_dst_summer_offset():
    # In January, Sydney is AEDT (+11). A 14:00-UTC start is 01:00 local; days=1
    # is the LOCAL Sunday it falls on. Verify it still lands on a Sunday.
    now = _epoch(2026, 1, 3, 20, 0)         # Sat evening, AEDT
    scheds = [{"days": 1, "start": "1400", "enabled": 1}]
    loc = _local(nc.compute_next_charge(scheds, SYD, now))
    assert loc.weekday() == 6, loc          # Sunday
    assert loc.hour == 1, loc               # 14:00 UTC == 01:00 AEDT


# ── plug_reminder_due ────────────────────────────────────────────────────────
@case
def test_plug_reminder_due_within_window_not_connected():
    now = 1_000_000
    nxt = now + 5 * 60                       # due in 5 min
    assert nc.plug_reminder_due(nxt, 10, False, now) is True


@case
def test_plug_reminder_not_due_outside_window():
    now = 1_000_000
    nxt = now + 30 * 60                       # 30 min out, lead 10
    assert nc.plug_reminder_due(nxt, 10, False, now) is False


@case
def test_plug_reminder_false_when_connected():
    now = 1_000_000
    nxt = now + 5 * 60
    assert nc.plug_reminder_due(nxt, 10, True, now) is False


@case
def test_plug_reminder_disabled_lead_zero():
    now = 1_000_000
    assert nc.plug_reminder_due(now + 300, 0, False, now) is False


@case
def test_plug_reminder_none_falls_back():
    now = 1_000_000
    # old firmware: no rem_lead -> None (caller uses firmware flag)
    assert nc.plug_reminder_due(now + 300, None, False, now) is None
    # no computed next charge yet -> None
    assert nc.plug_reminder_due(None, 10, False, now) is None
    assert nc.plug_reminder_due(0, 10, False, now) is None
    # unknown plug state -> None
    assert nc.plug_reminder_due(now + 300, 10, None, now) is None


@case
def test_plug_reminder_past_charge_not_due():
    now = 1_000_000
    assert nc.plug_reminder_due(now - 60, 10, False, now) is False


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
