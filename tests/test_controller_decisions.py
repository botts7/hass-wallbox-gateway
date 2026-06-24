"""Controller-glue tests — the decision logic that wires the pure helpers to
Home Assistant reads. Uses a minimal fake hass/coordinator (no
pytest-homeassistant dependency). HA must be importable (it is, in this env).

Covers this session's new glue: effective target (battery care), price-cap
gating, and surplus-source derivation feeding the controller. Run:
  py tests/test_controller_decisions.py
"""

import os
import sys
from datetime import timedelta

# Import the integration as a package (custom_components on path) so the
# package's relative imports resolve and stdlib `select` isn't shadowed.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

try:
    import wallbox_gateway.charge_assistant as ca_mod
    from wallbox_gateway import const as C
    from homeassistant.util import dt as dt_util
    _HA_OK = True
except Exception as e:  # pragma: no cover - environment without HA
    print(f"--- test_controller_decisions: SKIPPED (HA not importable: {e})")
    _HA_OK = False

CASES = []
def case(fn):
    CASES.append(fn); return fn


class FakeState:
    def __init__(self, state, attrs=None):
        self.state = state
        self.attributes = attrs or {}


class FakeStates:
    def __init__(self, mapping):
        self._m = mapping
    def get(self, eid):
        return self._m.get(eid)


class FakeCoord:
    def __init__(self, raw_status=None, meter=None):
        self.data = {"raw_status": raw_status or {"control_owner": "integration"},
                     "meter": meter or {}}
        self.client = None


class FakeHass:
    def __init__(self, states, coord):
        self.states = FakeStates(states)
        self.data = {C.DOMAIN: {"e1": coord}}
    def async_create_task(self, coro):
        try:
            coro.close()      # we assert on _set_charging, not the BLE coro
        except Exception:
            pass


class FakeEntry:
    def __init__(self, opts):
        self.entry_id = "e1"
        self.data = {}
        self.options = {C.CA_KEY: opts}


def build(opts, states):
    coord = FakeCoord()
    hass = FakeHass(states, coord)
    ca = ca_mod.ChargeAssistant(hass, FakeEntry(opts))
    ca._opts = dict(opts)
    ca._charge_switch = None
    ca._charging_sensor = None
    ca._plugged_in = lambda: True
    calls = []
    ca._set_charging = lambda on: calls.append(on)
    return ca, calls


# ── effective target (battery care) ─────────────────────────────────
@case
def test_target_pct_daily_vs_trip():
    until_future = (dt_util.utcnow() + timedelta(hours=6)).isoformat()
    until_past = (dt_util.utcnow() - timedelta(hours=1)).isoformat()
    ca, _ = build({C.CA_TARGET_PCT: 80, C.CA_TRIP_TARGET: 100,
                   C.CA_TRIP_UNTIL: until_future}, {})
    assert ca._target_pct() == 100.0
    ca._opts[C.CA_TRIP_UNTIL] = until_past
    assert ca._target_pct() == 80.0


# ── price-cap gating in autostart ───────────────────────────────────
def _autostart_opts(cap):
    return {
        C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
        C.CA_TARGET_AUTOSTART: True,
        C.CA_PRICE_ENTITY: "sensor.price", C.CA_PRICE_CAP: cap,
    }


@case
def test_autostart_blocked_above_price_cap():
    states = {"sensor.soc": FakeState("50"), "sensor.price": FakeState("0.45")}
    ca, calls = build(_autostart_opts(0.40), states)
    ca._eval_target()
    assert calls == [], f"should not start above cap, got {calls}"


@case
def test_autostart_allowed_below_price_cap():
    states = {"sensor.soc": FakeState("50"), "sensor.price": FakeState("0.30")}
    ca, calls = build(_autostart_opts(0.40), states)
    ca._eval_target()
    assert calls == [True], f"should start below cap, got {calls}"


@case
def test_trip_target_keeps_charging_past_daily():
    # SOC 90 is above the daily 80 (would stop) but below the active trip 100.
    until_future = (dt_util.utcnow() + timedelta(hours=6)).isoformat()
    opts = {
        C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
        C.CA_TARGET_AUTOSTART: True,
        C.CA_TRIP_TARGET: 100, C.CA_TRIP_UNTIL: until_future,
    }
    states = {"sensor.soc": FakeState("90")}
    ca, calls = build(opts, states)
    ca._eval_target()
    assert calls == [True], f"trip target should keep charging, got {calls}"


# ── surplus-source derivation ───────────────────────────────────────
@case
def test_surplus_value_grid_export_negative():
    opts = {C.CA_SURPLUS_SOURCE: "grid", C.CA_GRID_ENTITY: "sensor.grid",
            C.CA_GRID_EXPORT_NEGATIVE: True}
    ca, _ = build(opts, {"sensor.grid": FakeState("-1500")})
    assert ca._surplus_value() == 1500.0


@case
def test_surplus_value_solar_minus_load():
    opts = {C.CA_SURPLUS_SOURCE: "solar_load",
            C.CA_SOLAR_ENTITY: "sensor.solar", C.CA_LOAD_ENTITY: "sensor.load"}
    ca, _ = build(opts, {"sensor.solar": FakeState("4000"), "sensor.load": FakeState("1500")})
    assert ca._surplus_value() == 2500.0


# ── allowed charging window (composable) ────────────────────────────
def _hhmm(delta_h):
    return (dt_util.now() + timedelta(hours=delta_h)).strftime("%H:%M")


@case
def test_autostart_blocked_outside_window():
    # Window is 2–3h from now (now is outside it) → autostart must wait.
    opts = {C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
            C.CA_TARGET_AUTOSTART: True, C.CA_WINDOW_ENABLED: True,
            C.CA_WINDOW_START: _hhmm(2), C.CA_WINDOW_END: _hhmm(3)}
    ca, calls = build(opts, {"sensor.soc": FakeState("50")})
    ca._eval_target()
    assert calls == [], f"outside window should not start, got {calls}"


@case
def test_autostart_allowed_inside_window():
    # Window spans now (−1h..+1h) → autostart fires.
    opts = {C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
            C.CA_TARGET_AUTOSTART: True, C.CA_WINDOW_ENABLED: True,
            C.CA_WINDOW_START: _hhmm(-1), C.CA_WINDOW_END: _hhmm(1)}
    ca, calls = build(opts, {"sensor.soc": FakeState("50")})
    ca._eval_target()
    assert calls == [True], f"inside window should start, got {calls}"


@case
def test_smart_solar_starts_on_solar_even_outside_window():
    # Surplus available → charge from solar (free), ignoring the window.
    opts = {C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
            C.CA_SURPLUS_SOURCE: "entity", C.CA_SURPLUS_ENTITY: "sensor.surplus",
            C.CA_SURPLUS_START: 1.0, C.CA_WINDOW_ENABLED: True,
            C.CA_WINDOW_START: _hhmm(2), C.CA_WINDOW_END: _hhmm(3)}
    ca, calls = build(opts, {"sensor.soc": FakeState("50"), "sensor.surplus": FakeState("2000")})
    ca._eval_smart_solar()
    assert calls == [True], f"solar surplus should start, got {calls}"


@case
def test_smart_solar_grid_blocked_outside_window():
    # No surplus + outside the window → don't pull grid.
    opts = {C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
            C.CA_SURPLUS_SOURCE: "entity", C.CA_SURPLUS_ENTITY: "sensor.surplus",
            C.CA_SURPLUS_START: 1.0, C.CA_WINDOW_ENABLED: True,
            C.CA_WINDOW_START: _hhmm(2), C.CA_WINDOW_END: _hhmm(3)}
    ca, calls = build(opts, {"sensor.soc": FakeState("50"), "sensor.surplus": FakeState("0")})
    ca._eval_smart_solar()
    assert calls == [], f"no solar + outside window should wait, got {calls}"


@case
def test_smart_solar_grid_allowed_inside_window():
    # No surplus but inside the cheap window → grid top-up allowed.
    opts = {C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
            C.CA_SURPLUS_SOURCE: "entity", C.CA_SURPLUS_ENTITY: "sensor.surplus",
            C.CA_SURPLUS_START: 1.0, C.CA_WINDOW_ENABLED: True,
            C.CA_WINDOW_START: _hhmm(-1), C.CA_WINDOW_END: _hhmm(1)}
    ca, calls = build(opts, {"sensor.soc": FakeState("50"), "sensor.surplus": FakeState("0")})
    ca._eval_smart_solar()
    assert calls == [True], f"inside window grid top-up should start, got {calls}"


@case
def test_window_prestart_for_departure():
    # Outside the window, but departure is close and we need more time than
    # remains → pre-start (and flag the pricier charge).
    opts = {C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
            C.CA_WINDOW_ENABLED: True, C.CA_WINDOW_PRESTART: True,
            C.CA_WINDOW_START: _hhmm(5), C.CA_WINDOW_END: _hhmm(6),
            C.CA_DEPARTURE: _hhmm(1), C.CA_BATTERY_KWH: 60, C.CA_CHARGE_POWER_KW: 7.4}
    ca, _ = build(opts, {"sensor.soc": FakeState("50")})
    d = ca._window_decision(50.0, 80.0)
    assert d["allow_charge"] is True and d["reason"] == "prestart_for_departure"
    assert d["cost_warn"] is True


# ── native-schedule import: decode round-trips ──────────────────────
@case
def test_schedule_time_roundtrip():
    import wallbox_gateway.schedule as sch
    for t in ("00:00", "06:30", "23:15"):
        utc_int = sch._local_hhmm_to_utc_int(None, t)
        assert sch._utc_int_to_local_hhmm(utc_int) == t, f"round-trip failed for {t}"


@case
def test_schedule_days_roundtrip():
    import wallbox_gateway.schedule as sch
    arr = sch._days_array(["mon", "wed", "sun"])
    assert sch._days_from_array(arr) == ["mon", "wed", "sun"]


@case
def test_schedule_days_bitmask():
    # r_schs reads days back as a bitmask int (bit0=Mon..bit6=Sun).
    import wallbox_gateway.schedule as sch
    assert sch._days_from_array(127) == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert sch._days_from_array(32) == ["sat"]     # bit5
    assert sch._days_from_array(0) == []


@case
def test_decode_native_schedule_shape():
    import wallbox_gateway.schedule as sch
    row = {"sid": 2, "start": 1400, "stop": 2100, "days": [1, 1, 1, 1, 1, 0, 0],
           "mcr": 16, "enabled": 1, "target": {"type": 1, "value": 7000}}
    d = sch._decode_schedule(row)
    assert d["sid"] == 2 and d["max_current"] == 16 and d["enabled"] is True
    assert d["energy_target_kwh"] == 7.0
    assert d["days"] == ["mon", "tue", "wed", "thu", "fri"]
    assert d["start"] and d["stop"]
    assert sch._decode_schedule("not a dict") is None


def main():
    if not _HA_OK:
        return
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
