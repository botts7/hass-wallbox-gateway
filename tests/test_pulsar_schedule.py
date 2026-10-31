"""Schedules on the original (Zentri, no-WiFi) Pulsar, replaying the app snoop
from esp32-wallbox #68 (fw_v_ 375): four slots read with r_sch, a 12-character
w_sch with a 1-digit slot, delete = the slot all zero.

pulsar_schedule.py is HA-free; the schedule.py parts need HA and self-skip.
Run:  py tests/test_pulsar_schedule.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "custom_components", "wallbox_gateway"))
import pulsar_schedule as P  # noqa: E402

sys.path.insert(0, os.path.join(HERE, "..", "custom_components"))
try:
    from wallbox_gateway import schedule as S
    from homeassistant.exceptions import HomeAssistantError
    S._READBACK_DELAYS_S = (0, 0, 0)
    _HA_OK = True
except Exception as e:  # pragma: no cover
    print(f"--- test_pulsar_schedule (schedule.py part): SKIPPED ({e})")
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


_EMPTY = lambda sid: {"sid": sid, "start": "0000", "stop": "0000", "days": 0, "mcr": 0, "nrg": 0}


@case
def test_packing_matches_the_app():
    # Exact frames the Wallbox app sent in the snoop.
    assert P.wsch_par(0, 2300, 500, 127) == "023000500127"
    assert P.wsch_par(0, 0, 500, 127) == "000000500127"
    assert P.clear_par(0) == "000000000000"
    assert P.wsch_par(3, 2200, 600, 62) == "322000600062"
    assert _raises(P.wsch_par, 4, 0, 0, 0)
    assert _raises(P.wsch_par, 0, 0, 0, 128)


@case
def test_slot_choice():
    empty = [_EMPTY(i) for i in range(4)]
    assert P.pick_sid(None, empty) == 0            # the app used slot 0 first
    used0 = [{"sid": 0, "start": "2300", "stop": "0500", "days": 127}] + empty[1:]
    assert P.pick_sid(None, used0) == 1
    assert P.pick_sid(2, used0) == 2
    full = [{"sid": i, "start": "0100", "stop": "0200", "days": 1} for i in range(4)]
    assert _raises(P.pick_sid, None, full)
    assert _raises(P.pick_sid, 5, empty)


@case
def test_readback():
    rows = [{"sid": 0, "start": "2300", "stop": "0500", "days": 127}, _EMPTY(1)]
    assert P.row_applied(rows, 0, 2300, 500, 127)
    assert not P.row_applied(rows, 0, 2300, 600, 127)
    assert P.slot_cleared(rows, 1) and not P.slot_cleared(rows, 0)
    assert not P.slot_cleared([], 0), "unreadable slot is not proof of a delete"


class _OriginalPulsar:
    """r_sch per slot and the 12-char w_sch, as in the snoop."""
    def __init__(self):
        self.slots = {i: _EMPTY(i) for i in range(4)}
        self.calls = []

    async def command(self, req):
        self.calls.append((req["met"], req["par"]))
        if req["met"] == "r_schs":
            raise AssertionError("the original Pulsar doesn't answer r_schs")
        if req["met"] == "r_sch":
            return {"r": dict(self.slots[int(req["par"])])}
        if req["met"] == "w_sch":
            p = req["par"]
            assert len(p) == 12, p
            sid = int(p[0])
            self.slots[sid] = {"sid": sid, "start": p[1:5], "stop": p[5:9],
                               "days": int(p[9:12]), "mcr": 0, "nrg": 0}
            return {"r": None}
        return {"r": None}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@case
def test_create_and_delete_like_the_app():
    if not _HA_OK:
        return
    c = _OriginalPulsar()
    _run(S._set_pulsar(c, None, 2300, 500, ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]))
    assert ("w_sch", "023000500127") in c.calls, c.calls
    assert c.slots[0]["days"] == 127
    _run(S._delete_pulsar(c, [0]))
    assert ("w_sch", "000000000000") in c.calls
    assert c.slots[0]["days"] == 0


@case
def test_rows_come_from_r_sch_per_slot():
    if not _HA_OK:
        return
    c = _OriginalPulsar()
    rows = _run(S._read_pulsar_rows(c))
    assert [r["sid"] for r in rows] == [0, 1, 2, 3]
    assert [m for m, _ in c.calls] == ["r_sch"] * 4


@case
def test_full_and_bad_slot_errors():
    if not _HA_OK:
        return
    c = _OriginalPulsar()
    for i in range(4):
        c.slots[i] = {"sid": i, "start": "0100", "stop": "0200", "days": 1}
    try:
        _run(S._set_pulsar(c, None, 2300, 500, ["sun"]))
        assert False, "no free slot must raise"
    except HomeAssistantError:
        pass
    assert not any(m == "w_sch" for m, _ in c.calls)


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
