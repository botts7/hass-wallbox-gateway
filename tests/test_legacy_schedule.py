"""Unit tests for the pre-6.11 Pulsar Plus legacy schedule writes (#13).

The packed w_sch format was decoded and confirmed on a 6.7.41 Plus
(esp32-wallbox #50): <sid:2><start HHMM UTC><stop HHMM UTC><days:3>, days a
Sunday-first bitmask. Slot rules differ by model:
- 6.7.41 Plus: slots 1..4, never reports slot 0, writing slot 0 corrupts.
- 6.7.43 Pulsar Plus Socket (#13 field report): slot 0 is a real schedule that
  edits 1:1, and a write to an EMPTY slot is accepted then dropped.
legacy_schedule.py is HA-free; the schedule.py parts need HA and self-skip.
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
    from homeassistant.exceptions import HomeAssistantError
    S._READBACK_DELAYS_S = (0, 0, 0)   # no real waiting in tests
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


# ── pure helpers ────────────────────────────────────────────────────
@case
def test_no_dispatch_detection():
    assert L.is_no_dispatch({"error": {"code": 1, "message": "No dispatch method found"}})
    assert L.is_no_dispatch({"error": "no dispatch method found"})
    assert not L.is_no_dispatch({"error": {"code": 2, "message": "Busy"}})
    assert not L.is_no_dispatch({"error": {"code": 5}})   # original Pulsar: no text
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
    assert L.wsch_par(0, 2200, 800, 96) == "0022000800096"   # packing allows slot 0
    assert L.wsch_clear_par(3) == "0300000000000"
    assert _raises(L.wsch_par, 5, 1400, 2000, 1)
    assert _raises(L.wsch_par, 1, 1400, 2000, 128)


@case
def test_pick_legacy_sid():
    assert L.pick_legacy_sid(None, []) == 1
    assert L.pick_legacy_sid(None, [0, 2]) == 1   # new schedule never goes in slot 0
    assert L.pick_legacy_sid(None, [1, 2]) == 3
    assert L.pick_legacy_sid(3, [1, 2, 3]) == 3   # edit an existing slot
    assert L.pick_legacy_sid(0, [0, 2]) == 0      # #13 Socket: edit its real slot 0
    assert _raises(L.pick_legacy_sid, 0, [1, 2]), "no schedule in slot 0 -> refuse"
    assert _raises(L.pick_legacy_sid, 0, [])
    assert _raises(L.pick_legacy_sid, None, [1, 2, 3, 4])
    assert _raises(L.pick_legacy_sid, 7, [])


@case
def test_check_clear_sid():
    L.check_clear_sid(0, [0, 2])                  # existing slot 0 may be deleted
    L.check_clear_sid(3, [])
    assert _raises(L.check_clear_sid, 0, [2])
    assert _raises(L.check_clear_sid, 9, [])


@case
def test_readback_matching():
    rows = [{"sid": 0, "start": "2200", "stop": "0800", "days": 96},
            {"sid": 2, "start": 2200, "stop": 759, "days": [1, 1, 1, 1, 1, 0, 0]}]
    assert L.row_applied(rows, 0, 2200, 800, 96)
    assert L.row_applied(rows, 2, 2200, 759, 31)          # array days, int times
    assert not L.row_applied(rows, 2, 2200, 800, 31)       # stop didn't change
    assert not L.row_applied(rows, 1, 2200, 800, 31)       # slot not there
    assert L.slot_cleared(rows, 1)                         # absent = cleared
    assert not L.slot_cleared(rows, 0)
    assert L.slot_cleared([{"sid": 3, "start": 0, "stop": 0, "days": 0}], 3)


@case
def test_original_pulsar_detection():
    assert L.is_original_pulsar({"zentri": True, "chg_project": ""})
    assert L.is_original_pulsar({"zentri": True})
    # Older gateway firmware sets zentri for a Plus on that radio (#74): not it.
    assert not L.is_original_pulsar({"zentri": True, "chg_project": "prj08-pulsar-plus"})
    assert not L.is_original_pulsar({"zentri": False})
    assert not L.is_original_pulsar(None)


# ── schedule.py with a simulated charger ────────────────────────────
class _Charger:
    """r_schs / w_sch like a real unit. drop_new=True is the Pulsar Plus
    Socket (#13): a write to an empty slot is acknowledged and discarded."""
    def __init__(self, rows, drop_new=False, reply=None):
        self.rows = {r["sid"]: dict(r) for r in rows}
        self.drop_new = drop_new
        self.reply = reply
        self.calls = []

    async def command(self, req):
        self.calls.append((req["met"], req["par"]))
        if req["met"] == "r_schs":
            return {"r": {"schedules": list(self.rows.values())}}
        if req["met"] == "w_sch":
            if self.reply is not None:
                return self.reply
            p = req["par"]
            sid, start, stop, days = int(p[:2]), int(p[2:6]), int(p[6:10]), int(p[10:13])
            if sid in self.rows or not self.drop_new:
                if days == 0 and start == 0 and stop == 0:
                    self.rows.pop(sid, None)
                else:
                    self.rows[sid] = {"sid": sid, "start": f"{start:04d}",
                                      "stop": f"{stop:04d}", "days": days}
            return {"r": 0}
        return {"r": 0}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


_SOCKET = [{"sid": 0, "start": "2100", "stop": "0700", "days": 96, "mcr": 1, "enabled": 0},
           {"sid": 2, "start": "2100", "stop": "0700", "days": 31, "mcr": 1, "enabled": 0}]


@case
def test_set_legacy_edits_existing_and_confirms():
    if not _HA_OK:
        return
    c = _Charger(_SOCKET, drop_new=True)
    _run(S._set_legacy(c, 2, 2100, 459, ["sun", "mon", "tue", "wed", "thu"]))
    assert ("w_sch", "0221000459031") in c.calls, c.calls
    assert c.rows[2]["stop"] == "0459" and c.rows[0]["stop"] == "0700"
    _run(S._set_legacy(c, 0, 2100, 459, ["fri", "sat"]))   # slot 0 exists -> allowed
    assert c.rows[0]["stop"] == "0459"


@case
def test_set_legacy_dropped_new_schedule_raises():
    if not _HA_OK:
        return
    c = _Charger(_SOCKET, drop_new=True)
    try:
        _run(S._set_legacy(c, None, 1100, 2100, ["sun"]))
        assert False, "a write the charger dropped must not report success"
    except HomeAssistantError as e:
        assert "only allow editing existing" in str(e), e
    assert set(c.rows) == {0, 2}, "existing schedules untouched"


@case
def test_set_legacy_creates_where_supported():
    if not _HA_OK:
        return
    c = _Charger([], drop_new=False)                       # 6.7.41-style: creates
    _run(S._set_legacy(c, None, 1100, 2100, ["sun", "mon", "tue", "wed", "thu"]))
    assert c.rows[1]["days"] == 31


@case
def test_set_legacy_refuses_empty_slot_0_and_errors():
    if not _HA_OK:
        return
    c = _Charger([{"sid": 2, "start": "2100", "stop": "0700", "days": 31}])
    try:
        _run(S._set_legacy(c, 0, 1100, 2100, ["sun"]))
        assert False, "slot 0 with no schedule must be refused"
    except HomeAssistantError:
        pass
    assert not any(m == "w_sch" for m, _ in c.calls), "nothing written for slot 0"
    c = _Charger([], reply={"error": {"code": 2, "message": "Busy"}})
    try:
        _run(S._set_legacy(c, None, 1100, 2100, ["sun"]))
        assert False, "a refused w_sch must raise"
    except HomeAssistantError:
        pass


@case
def test_decode_hides_meaningless_current():
    if not _HA_OK:
        return
    assert S._decode_schedule(_SOCKET[0])["max_current"] is None   # mcr 1 on #13 unit
    assert S._decode_schedule({"sid": 1, "start": 1400, "stop": 2000, "days": 62,
                               "mcr": 16})["max_current"] == 16


@case
def test_device_entry_ids_compat():
    if not _HA_OK:
        return
    class New:   # HA 2026.10+: config_entry_id
        config_entry_id = "abc"
        @property
        def config_entries(self):
            raise AssertionError("deprecated property must not be read")
    class Old:   # HA 2024.12: only config_entries
        config_entries = {"x", }
    assert S._device_entry_ids(New()) == ["abc"]
    assert S._device_entry_ids(Old()) == ["x"]


@case
def test_import_snapshot_does_not_reload():
    if not _HA_OK:
        return
    from wallbox_gateway import options_need_reload
    base = {"poll_interval": 30, "charge_assistant": {"mode": "off"}}
    assert not options_need_reload(base, {**base, "imported_schedules": {"schedules": []}}),         "import_native_schedules must not reload the integration (#13)"
    assert options_need_reload(base, {**base, "poll_interval": 60})
    assert options_need_reload(base, {**base, "charge_assistant": {"mode": "target_soc"}})


@case
def test_service_waits_out_a_reload():
    if not _HA_OK:
        return
    S._RESOLVE_WAIT_S = 2.0
    state = {"n": 0}
    real = S._resolve_coord_entry
    def flaky(hass, device_id):
        state["n"] += 1
        if state["n"] < 3:   # entry still reloading for the first two tries
            raise HomeAssistantError("That device isn't a Wallbox Gateway charger.")
        return "coord", "entry"
    S._resolve_coord_entry = flaky
    try:
        assert _run(S._await_coord_entry(None, "dev")) == ("coord", "entry")
    finally:
        S._resolve_coord_entry = real


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
