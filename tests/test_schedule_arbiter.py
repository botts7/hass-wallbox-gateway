"""Unit tests for the native-schedule arbiter's coexistence scoping (#152).

HA-light: the arbiter module imports Home Assistant at top, so these self-skip
when HA isn't importable (like test_solar_switch). Where it imports, we build a
NativeScheduleArbiter via __new__ (bypassing the Store-creating __init__), stub
the schedule read + Store load/save, and drive _take_control / _release_control
against a fake client that records the s_sch writes.
Run:  py tests/test_schedule_arbiter.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))
import select  # noqa: F401,E402  (preload before the package shadows it)

try:
    from wallbox_gateway.schedule_arbiter import NativeScheduleArbiter
    _HA_OK = True
except Exception as e:  # pragma: no cover - environment without HA
    print(f"--- test_schedule_arbiter: SKIPPED (HA not importable: {e})")
    _HA_OK = False

CASES = []
def case(fn):
    CASES.append(fn); return fn


def _m(h, mn=0):
    return h * 60 + mn


class FakeClient:
    """Records s_sch writes; answers r_schs from a fixed row list."""
    def __init__(self, rows):
        self._rows = rows
        self.writes = []  # list of (sid, enabled) from s_sch calls

    async def command(self, req):
        met = req.get("met")
        if met == "r_schs":
            return {"r": {"schedules": self._rows}}
        if met == "s_sch":
            par = json.loads(req["par"])
            for e in par["schedules"]:
                self.writes.append((int(e["sid"]), int(e["enabled"])))
            return {"r": {"ok": 1}}
        raise AssertionError(f"unexpected BAPI met {met!r}")


def _row(sid, start, stop, enabled=1):
    """An r_schs READ row (start/stop are HHMM strings, days a bitmask)."""
    return {"sid": sid, "start": f"{start:04d}", "stop": f"{stop:04d}",
            "days": 127, "mcr": 16, "type": 0, "enabled": enabled,
            "target": {"type": 0, "value": 0}, "repeat": 1}


def _arbiter(rows):
    """A NativeScheduleArbiter with a fake client + in-memory Store state,
    without running the real (Store-creating) __init__."""
    a = NativeScheduleArbiter.__new__(NativeScheduleArbiter)
    a._client = FakeClient(rows)
    a._busy = False
    a._mem = {"controlling": False, "snapshot": {}}

    async def _load():
        return {"controlling": a._mem["controlling"],
                "snapshot": dict(a._mem["snapshot"])}

    async def _save(state):
        a._mem = {"controlling": bool(state.get("controlling")),
                  "snapshot": dict(state.get("snapshot") or {})}

    a._load = _load
    a._save = _save
    return a


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── _overlaps_window (pure decision) ────────────────────────────────
@case
def test_overlaps_window_no_window_disables_all():
    # active_window None -> legacy: every schedule is in scope.
    assert NativeScheduleArbiter._overlaps_window(_row(1, 0, 600), None) is True


@case
def test_overlaps_window_night_preserved_day_disabled():
    day = (_m(9), _m(16))
    assert NativeScheduleArbiter._overlaps_window(_row(1, 0, 600), day) is False   # 00:00–06:00
    assert NativeScheduleArbiter._overlaps_window(_row(2, 1000, 1400), day) is True  # 10:00–14:00


@case
def test_overlaps_window_unparseable_disabled_failsafe():
    day = (_m(9), _m(16))
    bad = {"sid": 9, "start": "nope", "stop": "0600", "enabled": 1}
    assert NativeScheduleArbiter._overlaps_window(bad, day) is True  # can't prove night -> disable


# ── _take_control scoping ───────────────────────────────────────────
@case
def test_take_control_scoped_keeps_night_schedule():
    # sid 1 = 00:00–06:00 night, sid 2 = 10:00–14:00 day. Day window 09:00–16:00.
    a = _arbiter([_row(1, 0, 600), _row(2, 1000, 1400)])
    ok = _run(a.async_reconcile(True, active_window=(_m(9), _m(16))))
    assert ok is True
    # Only the day schedule (sid 2) was disabled; the night one is untouched.
    assert a._client.writes == [(2, 0)]
    assert set(a._mem["snapshot"]) == {"2"}
    assert a._mem["controlling"] is True


@case
def test_take_control_no_window_disables_all():
    a = _arbiter([_row(1, 0, 600), _row(2, 1000, 1400)])
    ok = _run(a.async_reconcile(True))  # legacy: no active_window
    assert ok is True
    assert sorted(a._client.writes) == [(1, 0), (2, 0)]
    assert set(a._mem["snapshot"]) == {"1", "2"}


@case
def test_release_restores_only_scoped_schedule():
    # Take control scoped (disables sid 2 only), then release restores sid 2.
    a = _arbiter([_row(1, 0, 600), _row(2, 1000, 1400)])
    _run(a.async_reconcile(True, active_window=(_m(9), _m(16))))
    a._client.writes.clear()
    ok = _run(a.async_reconcile(False))
    assert ok is True
    assert a._client.writes == [(2, 1)]      # re-enabled the one we disabled
    assert a._mem["controlling"] is False
    assert a._mem["snapshot"] == {}


def main():
    if not _HA_OK:
        return
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
