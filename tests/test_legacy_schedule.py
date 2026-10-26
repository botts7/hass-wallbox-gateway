"""Unit tests for the pre-6.11 Pulsar Plus legacy schedule writes (#13).

The packed w_sch format was decoded and confirmed on a 6.7.41 Plus
(esp32-wallbox #50): <sid:2><start HHMM UTC><stop HHMM UTC><days:3>, days a
Sunday-first bitmask, slots 1..4 (slot 0 corrupts schedules on that firmware).
legacy_schedule.py is HA-free; the schedule.py fallback needs HA and self-skips.
Run:  py tests/test_legacy_schedule.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "custom_components", "wallbox_gateway"))
import legacy_schedule as L  # noqa: E402

sys.path.insert(0, os.path.join(HERE, "..", "custom_components"))
try:
    from wallbox_gateway import schedule as S
    _HA_OK = True
except Exception as e:  # pragma: no cover - environment without HA
    print(f"--- test_legacy_schedule (schedule.py part): SKIPPED ({e})")
    _HA_OK = False

CASES = []
def case(fn):
    CASES.append(fn); return fn


def _raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


@case
def test_no_dispatch_detection():
    assert L.is_no_dispatch({"error": {"code": 1, "message": "No dispatch method found"}})
    assert L.is_no_dispatch({"error": "no dispatch method found"})
    assert not L.is_no_dispatch({"error": {"code": 2, "message": "Busy"}})
    assert not L.is_no_dispatch({"r": 0})
    assert not L.is_no_dispatch(None)


@case
def test_days_bitmask_sunday_first():
    assert L.days_bitmask(["sun"]) == 1
    assert L.days_bitmask(["fri"]) == 32
    assert L.days_bitmask(["mon", "tue", "wed", "thu", "fri"]) == 62   # app's Mon-Fri
    assert L.days_bitmask(["sun", "mon", "tue", "wed", "thu"]) == 31  # the #13 repro
    assert L.days_bitmask(["fri", "FRI"]) == 32                        # duplicates once


@case
def test_wsch_par_packing():
    # 2-digit sid, 4-digit times (leading zeros kept), 3-digit days.
    assert L.wsch_par(2, 1400, 2000, 62) == "0214002000062"
    assert L.wsch_par(1, 5, 900, 1) == "0100050900001"
    assert L.wsch_clear_par(3) == "0300000000000"
    assert _raises(L.wsch_par, 0, 1400, 2000, 1), "slot 0 must never be written"
    assert _raises(L.wsch_par, 5, 1400, 2000, 1)
    assert _raises(L.wsch_par, 1, 1400, 2000, 128)


@case
def test_pick_legacy_sid():
    assert L.pick_legacy_sid(None, []) == 1
    assert L.pick_legacy_sid(None, [0, 2]) == 1   # #13 unit reports sid 0 and 2
    assert L.pick_legacy_sid(None, [1, 2]) == 3
    assert L.pick_legacy_sid(3, [1, 2, 3]) == 3   # edit an existing slot
    assert _raises(L.pick_legacy_sid, None, [1, 2, 3, 4])
    assert _raises(L.pick_legacy_sid, 0, [])
    assert _raises(L.pick_legacy_sid, 7, [])


class _Client:
    """r_schs from fixed sids; records every BAPI call."""
    def __init__(self, sids, reply=None):
        self._sids = sids
        self._reply = reply if reply is not None else {"r": 0}
        self.calls = []
    async def command(self, req):
        self.calls.append((req["met"], req["par"]))
        if req["met"] == "r_schs":
            return {"r": {"schedules": [{"sid": s} for s in self._sids]}}
        return self._reply


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@case
def test_set_legacy_writes_packed_wsch():
    if not _HA_OK:
        return
    c = _Client([0, 2])
    _run(S._set_legacy(c, None, 1100, 2100, ["sun", "mon", "tue", "wed", "thu"]))
    assert c.calls[-1] == ("w_sch", "0111002100031"), c.calls


@case
def test_set_legacy_rejects_slot_0_and_charger_errors():
    if not _HA_OK:
        return
    from homeassistant.exceptions import HomeAssistantError
    c = _Client([])
    try:
        _run(S._set_legacy(c, 0, 1100, 2100, ["sun"]))
        assert False, "slot 0 must be refused"
    except HomeAssistantError:
        pass
    assert not any(m == "w_sch" for m, _ in c.calls), "nothing written for slot 0"
    c = _Client([], reply={"error": {"code": 2, "message": "Busy"}})
    try:
        _run(S._set_legacy(c, None, 1100, 2100, ["sun"]))
        assert False, "a refused w_sch must raise"
    except HomeAssistantError:
        pass


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
