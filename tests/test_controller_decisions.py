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
def test_smart_solar_charges_above_target_on_solar():
    # Above the SOC target but solar surplus available → keep grabbing free solar
    # (the target only caps GRID top-up, never free solar).
    opts = {C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
            C.CA_SURPLUS_SOURCE: "entity", C.CA_SURPLUS_ENTITY: "sensor.surplus",
            C.CA_SURPLUS_START: 1.0}
    ca, calls = build(opts, {"sensor.soc": FakeState("85"), "sensor.surplus": FakeState("2000")})
    ca._eval_smart_solar()
    assert calls == [True], f"solar should charge past target, got {calls}"


@case
def test_smart_solar_stops_above_target_without_solar():
    # Above target with no surplus → stop the (grid) charge.
    opts = {C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
            C.CA_SURPLUS_SOURCE: "entity", C.CA_SURPLUS_ENTITY: "sensor.surplus",
            C.CA_SURPLUS_START: 1.0}
    ca, calls = build(opts, {"sensor.soc": FakeState("85"), "sensor.surplus": FakeState("0")})
    ca._is_charging = lambda: True
    ca._we_started = True
    ca._eval_smart_solar()
    assert calls == [False], f"no solar above target should stop, got {calls}"


@case
def test_smart_solar_stops_at_solar_ceiling():
    # At the solar ceiling (default 100%) even surplus stops it — battery full.
    opts = {C.CA_SOC_ENTITY: "sensor.soc", C.CA_TARGET_PCT: 80,
            C.CA_SURPLUS_SOURCE: "entity", C.CA_SURPLUS_ENTITY: "sensor.surplus",
            C.CA_SURPLUS_START: 1.0}
    ca, calls = build(opts, {"sensor.soc": FakeState("100"), "sensor.surplus": FakeState("2000")})
    ca._is_charging = lambda: True
    ca._we_started = True
    ca._eval_smart_solar()
    assert calls == [False], f"at solar ceiling should stop, got {calls}"


@case
def test_solar_ceiling_default_and_custom():
    ca, _ = build({}, {})
    assert ca._solar_ceiling() == 100.0
    ca._opts[C.CA_SOLAR_MAX_SOC] = 90
    assert ca._solar_ceiling() == 90.0


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


# ── reminder "what will happen" plan clause ─────────────────────────
@case
def test_plan_clause_target_autostart_plug_aware():
    # Plugged in (build() default) → "now that it's plugged in".
    ca, _ = build({C.CA_MODE: C.MODE_TARGET, C.CA_TARGET_PCT: 80,
                   C.CA_TARGET_AUTOSTART: True}, {})
    assert ca._plan_clause() == "will charge to 80% now that it's plugged in"
    # Unplugged → "as soon as you plug in" (the real nudge scenario).
    ca._plugged_in = lambda: False
    assert ca._plan_clause() == "will charge to 80% as soon as you plug in"


@case
def test_plan_clause_target_autostart_window():
    # Window wording is plug-state-independent (no contradiction either way).
    ca, _ = build({C.CA_MODE: C.MODE_TARGET, C.CA_TARGET_PCT: 80,
                   C.CA_TARGET_AUTOSTART: True, C.CA_WINDOW_ENABLED: True,
                   C.CA_WINDOW_START: "00:00", C.CA_WINDOW_END: "06:00"}, {})
    assert ca._plan_clause() == "will charge to 80% in the 00:00–06:00 window"


@case
def test_plan_clause_target_manual():
    ca, _ = build({C.CA_MODE: C.MODE_TARGET, C.CA_TARGET_PCT: 80,
                   C.CA_TARGET_AUTOSTART: False}, {})
    # Plugged in (build() default) → already-plugged manual wording.
    assert ca._plan_clause() == "plugged in — tap Start to charge to 80%"
    ca._plugged_in = lambda: False
    assert ca._plan_clause() == "plug in, then tap Start to charge to 80%"


@case
def test_plan_clause_solar_and_smart_solar():
    ca, _ = build({C.CA_MODE: C.MODE_SOLAR}, {})
    assert "spare solar" in ca._plan_clause()
    ca2, _ = build({C.CA_MODE: C.MODE_SMART_SOLAR, C.CA_TARGET_PCT: 90}, {})
    assert ca2._plan_clause().startswith("will use solar first")


@case
def test_plan_clause_reminder_only_is_none():
    # No acting strategy (reminder-only / off) → None, so the caller falls back
    # to the charger's native next-charge time.
    assert build({C.CA_MODE: C.MODE_REMINDER}, {})[0]._plan_clause() is None
    assert build({C.CA_MODE: C.MODE_OFF}, {})[0]._plan_clause() is None


# ── auto-start grace period + managed override ──────────────────────
@case
def test_grace_minutes_parse():
    assert build({C.CA_AUTOSTART_GRACE_MIN: "5"}, {})[0]._grace_minutes() == 5
    assert build({}, {})[0]._grace_minutes() == 0
    assert build({C.CA_AUTOSTART_GRACE_MIN: "bad"}, {})[0]._grace_minutes() == 0


@case
def test_grace_defers_then_fires():
    # With a grace period the autostart is scheduled, not immediate; firing the
    # scheduled callback then starts.
    sched = {}
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (
        sched.update(delay=delay, cb=cb) or (lambda: sched.update(cancelled=True))
    )
    try:
        ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                           C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True,
                           C.CA_AUTOSTART_GRACE_MIN: 5},
                          {"sensor.soc": FakeState("50")})
        ca._eval_target()
        assert calls == [], f"grace should defer, got {calls}"
        assert sched.get("delay") == 300 and ca._grace_pending is not None
        sched["cb"](None)                      # grace timer fires
        assert calls == [True], f"grace fire should start, got {calls}"
        assert ca._grace_pending is None
    finally:
        ca_mod.async_call_later = orig


@case
def test_grace_cancel_blocks_start():
    sched = {}
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (
        sched.update(cb=cb) or (lambda: None)
    )
    try:
        ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                           C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True,
                           C.CA_AUTOSTART_GRACE_MIN: 5},
                          {"sensor.soc": FakeState("50")})
        ca._eval_target()
        assert ca._grace_pending is not None
        ca._cancel_grace("test")
        assert ca._grace_pending is None and calls == []
    finally:
        ca_mod.async_call_later = orig


@case
def test_autostart_suppressed_blocks_start():
    ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                       C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True},
                      {"sensor.soc": FakeState("50")})
    ca._autostart_suppress_until = dt_util.utcnow() + timedelta(minutes=30)
    ca._eval_target()
    assert calls == [], f"'Not now' suppression should block start, got {calls}"


@case
def test_finish_charge_stops_and_verifies():
    sched = {}
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (
        sched.update(delay=delay, cb=cb) or (lambda: None))
    try:
        ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                           C.CA_TARGET_PCT: 80}, {})
        ca._finish_charge(80.0, 80.0)
        assert calls == [False], f"finish should issue stop, got {calls}"
        assert ca._reached_target is True
        assert ca._finishing is not None and sched.get("delay") == 18
        # build()'s _is_charging is False → verify confirms the stop took.
        sched["cb"](None)
        assert ca._finishing is None, "confirmed stop should clear finishing"
    finally:
        ca_mod.async_call_later = orig


@case
def test_finish_retries_when_stop_ignored():
    sched = {}
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (sched.update(cb=cb) or (lambda: None))
    try:
        ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                           C.CA_TARGET_PCT: 80}, {})
        # Charger keeps ignoring Stop → live power stays high (finish-verify reads
        # charger_realtime.cp, not just _is_charging, to ignore a ramp-down tail).
        ca._coordinator().data["charger_realtime"] = {"cp": 7.0}
        ca._finish_charge(80.0, 80.0)
        assert calls == [False]
        sched["cb"](None); assert calls == [False, False]           # retry 2
        sched["cb"](None); assert calls == [False, False, False]    # retry 3
        sched["cb"](None)                                           # cap hit → give up
        assert ca._finishing is None
        assert calls == [False, False, False], f"no stops past the cap, got {calls}"
    finally:
        ca_mod.async_call_later = orig


@case
def test_finish_ignores_rampdown_tail():
    # A small ramp-down power tail (0.3 kW) after Stop must read as STOPPED, not
    # "charger ignored the Stop" — otherwise it false-alarms (the user hit this).
    sched = {}
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (sched.update(cb=cb) or (lambda: None))
    try:
        ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                           C.CA_TARGET_PCT: 80}, {})
        ca._coordinator().data["charger_realtime"] = {"cp": 0.3}  # ramp-down tail
        ca._finish_charge(80.0, 80.0)
        assert calls == [False]
        sched["cb"](None)                       # verify fires
        assert ca._finishing is None, "ramp-down tail should count as stopped"
        assert calls == [False], "must NOT retry a Stop that already took"
    finally:
        ca_mod.async_call_later = orig


@case
def test_initial_plugin_starts_within_deadband():
    # Fresh plug-in at 77% with target 80% (only 3% gap) MUST start — the wide
    # 5% deadband only applies after we've already reached target.
    ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                       C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True},
                      {"sensor.soc": FakeState("77")})
    ca._eval_target()
    assert calls == [True], f"fresh plug-in below target should start, got {calls}"


@case
def test_no_reflap_after_target():
    # After reaching target, a small drop (77 vs 80) must NOT restart (anti-flap).
    ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                       C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True},
                      {"sensor.soc": FakeState("77")})
    ca._reached_target = True
    ca._eval_target()
    assert calls == [], f"within 5% deadband after target should not restart, got {calls}"


@case
def test_unplug_resets_reached_target():
    ca, _ = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                   C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True},
                  {"sensor.soc": FakeState("77")})
    ca._reached_target = True
    ca._plugged_in = lambda: False
    ca._eval_target()
    assert ca._reached_target is False, "unplug should reset the anti-flap flag"


@case
def test_is_paused_reads_gen():
    ca, _ = build({C.CA_MODE: C.MODE_TARGET}, {})
    coord = ca._coordinator()
    coord.data["raw_status"] = {"gen": 0}
    assert ca._is_paused() is False
    coord.data["raw_status"] = {"gen": 1}
    assert ca._is_paused() is True


@case
def test_charger_adapter_eco_and_resume():
    import asyncio
    import json as _json
    from wallbox_gateway.charger_control import WallboxGatewayCharger, ECO_DISABLED

    sent = []

    class FakeClient:
        async def get(self, url): sent.append(("get", url))
        async def bapi(self, met, par=None, wait_ms=None): sent.append(("bapi", met, par))

    class FakeCoord2:
        def __init__(self):
            self.client = FakeClient()
            self.data = {"eco_smart": {"mode": 1, "power_pct": 80}}

    ch = WallboxGatewayCharger(FakeCoord2())
    assert ch.eco_mode() == 1                       # Full Green

    async def _run():
        await ch.set_eco_mode(ECO_DISABLED)
        await ch.resume()
    asyncio.run(_run())

    eco_calls = [c for c in sent if c[0] == "bapi" and c[1] == "s_ecos"]
    assert eco_calls and _json.loads(eco_calls[0][2])["esm"] == 0, sent
    assert any(c[0] == "get" and "resume" in c[1] for c in sent), sent


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


# ── window GOVERNS: bound the charge by the cheap window ─────────────
@case
def test_window_jit_waits_early_in_window():
    # In the cheap window but plenty of time before the window END → JIT waits
    # (charge as late as possible within the cheap hours).
    opts = {C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
            C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True,
            C.CA_WINDOW_ENABLED: True,
            C.CA_WINDOW_START: _hhmm(-1), C.CA_WINDOW_END: _hhmm(6),
            C.CA_BATTERY_KWH: 80, C.CA_CHARGE_POWER_KW: 6.8}
    ca, calls = build(opts, {"sensor.soc": FakeState("55")})
    ca._eval_target()
    assert calls == [], f"JIT should wait early in window, got {calls}"


@case
def test_window_jit_starts_when_late():
    # Window end is only minutes away and target not reached → start now.
    opts = {C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
            C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True,
            C.CA_WINDOW_ENABLED: True,
            C.CA_WINDOW_START: _hhmm(-5), C.CA_WINDOW_END: _hhmm(0.15),
            C.CA_BATTERY_KWH: 80, C.CA_CHARGE_POWER_KW: 6.8}
    ca, calls = build(opts, {"sensor.soc": FakeState("55")})
    ca._eval_target()
    assert calls == [True], f"late in window should start, got {calls}"


@case
def test_window_stops_when_outside_no_overrun():
    # Charging but now OUTSIDE the window with overrun OFF → stop (bounded).
    sched = {}
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (
        sched.update(delay=delay, cb=cb) or (lambda: None))
    try:
        opts = {C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True,
                C.CA_WINDOW_ENABLED: True, C.CA_WINDOW_OVERRUN: False,
                C.CA_WINDOW_START: _hhmm(2), C.CA_WINDOW_END: _hhmm(3)}
        ca, calls = build(opts, {"sensor.soc": FakeState("60")})
        ca._is_charging = lambda: True
        ca._we_started = True
        ca._eval_target()
        assert calls == [False], f"outside window should stop, got {calls}"
    finally:
        ca_mod.async_call_later = orig


@case
def test_window_overrun_keeps_charging_outside():
    # Outside the window but overrun ON → keep charging to target.
    opts = {C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
            C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True,
            C.CA_WINDOW_ENABLED: True, C.CA_WINDOW_OVERRUN: True,
            C.CA_WINDOW_START: _hhmm(2), C.CA_WINDOW_END: _hhmm(3)}
    ca, calls = build(opts, {"sensor.soc": FakeState("60")})
    ca._is_charging = lambda: True
    ca._we_started = True
    ca._eval_target()
    assert calls == [], f"overrun should keep charging, got {calls}"


@case
def test_departure_jit_no_window_still_starts():
    # No window, departure set + late enough → JIT starts (unchanged path).
    opts = {C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
            C.CA_TARGET_PCT: 80, C.CA_DEPARTURE: _hhmm(0.2),
            C.CA_BATTERY_KWH: 80, C.CA_CHARGE_POWER_KW: 6.8}
    ca, calls = build(opts, {"sensor.soc": FakeState("55")})
    ca._eval_target()
    assert calls == [True], f"departure JIT (no window) should start, got {calls}"


# ── start-verify watchdog: re-assert an Eco-Smart-re-queued charge ───
@case
def test_start_verify_reasserts_on_eco_requeue():
    sched = {}
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (
        sched.update(delay=delay, cb=cb) or (lambda: None))
    try:
        ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                           C.CA_TARGET_PCT: 80}, {"sensor.soc": FakeState("55")})
        ca._we_started = True
        ca._is_charging = lambda: False        # charger re-queued it
        ca._schedule_start_verify("test", 80.0)
        assert ca._starting is not None and sched.get("delay") == 12
        sched["cb"](None)                      # watchdog fires
        assert calls == [True], f"should re-assert start, got {calls}"
        assert ca._starting["attempts"] == 2
    finally:
        ca_mod.async_call_later = orig


@case
def test_start_verify_clears_when_charging():
    sched = {}
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (sched.update(cb=cb) or (lambda: None))
    try:
        ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                           C.CA_TARGET_PCT: 80}, {"sensor.soc": FakeState("55")})
        ca._we_started = True
        ca._is_charging = lambda: True         # start held
        ca._schedule_start_verify("test", 80.0)
        sched["cb"](None)
        assert ca._starting is None and calls == [], f"charging → no re-assert, got {calls}"
    finally:
        ca_mod.async_call_later = orig


@case
def test_start_verify_gives_up_after_retries():
    sched = {}
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (sched.update(cb=cb) or (lambda: None))
    try:
        ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                           C.CA_TARGET_PCT: 80}, {"sensor.soc": FakeState("55")})
        ca._we_started = True
        ca._is_charging = lambda: False
        ca._schedule_start_verify("test", 80.0)
        sched["cb"](None)      # attempt 2 (re-assert)
        sched["cb"](None)      # attempt 3 (re-assert)
        sched["cb"](None)      # cap → give up
        assert ca._starting is None
        assert calls == [True, True], f"two re-asserts then give up, got {calls}"
    finally:
        ca_mod.async_call_later = orig


@case
def test_start_verify_cancelled_by_finish():
    # An intentional stop (finish) must cancel a pending re-assert.
    sched = {}
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (sched.update(cb=cb) or (lambda: None))
    try:
        ca, calls = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                           C.CA_TARGET_PCT: 80}, {"sensor.soc": FakeState("80")})
        ca._we_started = True
        ca._schedule_start_verify("test", 80.0)
        assert ca._starting is not None
        ca._finish_charge(80.0, 80.0)          # stop supersedes the re-assert
        assert ca._starting is None, "finish should cancel the start-verify"
    finally:
        ca_mod.async_call_later = orig


# ── next-start estimate (display) ───────────────────────────────────
@case
def test_next_start_estimate_window_scheduled():
    # Early in the window with an energy model → a future scheduled start time.
    opts = {C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
            C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True,
            C.CA_WINDOW_ENABLED: True,
            C.CA_WINDOW_START: _hhmm(-1), C.CA_WINDOW_END: _hhmm(6),
            C.CA_BATTERY_KWH: 80, C.CA_CHARGE_POWER_KW: 6.8}
    ca, _ = build(opts, {"sensor.soc": FakeState("55")})
    est = ca.next_start_estimate()
    assert est["state"] == "scheduled" and est["time"] is not None, est
    assert "by" in est["reason"], est


@case
def test_next_start_estimate_charging_and_target_and_off():
    ca, _ = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                   C.CA_TARGET_PCT: 80}, {"sensor.soc": FakeState("55")})
    ca._is_charging = lambda: True
    assert ca.next_start_estimate()["state"] == "charging"
    ca2, _ = build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                    C.CA_TARGET_PCT: 80}, {"sensor.soc": FakeState("85")})
    assert ca2.next_start_estimate()["state"] == "target_reached"
    assert build({C.CA_MODE: C.MODE_OFF}, {})[0].next_start_estimate()["state"] == "off"


# ── solar-available reminder + "only when home" condition ───────────
@case
def test_solar_remind_rising_edge():
    # Surplus crossing the threshold fires once; re-arms after it clearly drops.
    opts = {C.CA_SURPLUS_SOURCE: "entity", C.CA_SURPLUS_ENTITY: "sensor.surplus"}
    ca, _ = build(opts, {"sensor.surplus": FakeState("2.0")})
    ca._rem = {C.CA_SOLAR_REMIND_KW: 1.4}
    fired = []
    ca._maybe_remind = lambda src: fired.append(src)
    ca._eval_solar_remind()                                   # 2.0 >= 1.4 → fire
    assert fired == ["solar"] and ca._solar_reminded is True
    ca._eval_solar_remind()                                   # still high → no re-fire
    assert fired == ["solar"]
    ca.hass.states._m["sensor.surplus"] = FakeState("0.5")    # below 0.7×1.4 → re-arm
    ca._eval_solar_remind()
    assert ca._solar_reminded is False
    ca.hass.states._m["sensor.surplus"] = FakeState("2.0")    # rises again, but within cooldown
    ca._eval_solar_remind()
    assert fired == ["solar"], "anti-spam cooldown blocks the immediate re-nudge"
    ca._solar_last_remind = dt_util.utcnow() - timedelta(hours=5)  # cooldown elapsed
    ca._solar_reminded = False
    ca._eval_solar_remind()
    assert fired == ["solar", "solar"], "fires again once cooldown passes"


@case
def test_home_ok_gate():
    ca, _ = build({}, {"person.me": FakeState("home")})
    ca._rem = {}
    assert ca._home_ok() is True                              # no gate set
    ca._rem = {C.CA_HOME_ENTITY: "person.me"}
    assert ca._home_ok() is True                              # home
    ca.hass.states._m["person.me"] = FakeState("not_home")
    assert ca._home_ok() is False                             # away → blocked
    ca.hass.states._m["person.me"] = FakeState("unknown")
    assert ca._home_ok() is True                              # unknown → don't suppress


@case
def test_maybe_remind_respects_home_gate():
    ca, _ = build({}, {"person.me": FakeState("not_home")})
    ca._rem = {C.CA_HOME_ENTITY: "person.me"}
    ca._plugged_in = lambda: False        # unplugged (would otherwise nudge)
    ca._arm_escalation = lambda: None     # no real timers
    ca._escalations_left = 0
    ca._maybe_remind("solar")
    assert ca._escalations_left == 0, "not home → no nudge"
    ca.hass.states._m["person.me"] = FakeState("home")
    ca._maybe_remind("solar")
    assert ca._escalations_left == ca_mod._MAX_ESCALATIONS, "home → nudge"


# ── auto-resume Eco-Smart/schedule after a manual charge ────────────
@case
def test_auto_resume_when_paused_and_idle():
    ca, _ = build({C.CA_MODE: C.MODE_OFF}, {})
    ca._coordinator().data["raw_status"] = {"control_owner": "integration", "gen": 1}  # paused
    ca._is_charging = lambda: False
    ca._maybe_auto_resume(False)                  # 1st tick just arms the timer
    assert ca._auto_resume_since is not None and ca._auto_resume_last is None
    ca._auto_resume_since = dt_util.utcnow() - timedelta(minutes=5)  # delay elapsed
    ca._maybe_auto_resume(False)
    assert ca._auto_resume_last is not None, "paused+idle past the delay should resume"


@case
def test_auto_resume_skips():
    def armed(opts, gen, charging, controlling, optsroot=None):
        ca, _ = build(opts, {})
        if optsroot is not None:
            ca.entry.options = optsroot
        ca._coordinator().data["raw_status"] = {"gen": gen}
        ca._is_charging = lambda: charging
        ca._auto_resume_since = dt_util.utcnow() - timedelta(minutes=5)
        ca._maybe_auto_resume(controlling)
        return ca._auto_resume_last
    assert armed({}, 0, False, False) is None, "not paused → no resume"
    assert armed({}, 1, True, False) is None, "charging → no resume"
    assert armed({}, 1, False, True) is None, "we're the controller → no resume"
    assert armed({}, 1, False, False, optsroot={C.CA_KEY: {}, C.CA_AUTO_RESUME: False}) is None, \
        "disabled via option → no resume"


# ── commute-based adaptive target ───────────────────────────────────
@case
def test_commute_avg_use_and_target():
    opts = {C.CA_MODE: C.MODE_TARGET, C.CA_BATTERY_KWH: 80, C.CA_TARGET_PCT: 80,
            C.CA_COMMUTE_ENABLED: True, C.CA_COMMUTE_RESERVE: 20,
            C.CA_COMMUTE_MARGIN: 10, C.CA_COMMUTE_COVER_DAYS: 1,
            C.CA_COMMUTE_WINDOW_DAYS: 7, C.CA_SOC_ENTITY: "sensor.soc"}
    ca, _ = build(opts, {"sensor.soc": FakeState("50")})
    now = dt_util.utcnow().timestamp()
    ca._coordinator().data["charge_log"] = [
        {"start": now - 1 * 86400, "wh": 16000},
        {"start": now - 2 * 86400, "wh": 16000},
        {"start": now - 4 * 86400, "wh": 32000},
        {"start": now - 6 * 86400, "wh": 16000},     # 80 kWh over a ~6-day span
    ]
    avg = ca._avg_daily_use_kwh()
    assert avg is not None and 12 < avg < 15, f"~13.3 kWh/day expected, got {avg}"
    t = ca._commute_target()                          # 20 + 16.7 + 10 ≈ 46.7
    assert t is not None and 42 < t < 52, t
    assert abs(ca._target_pct() - t) < 0.01, "commute target flows into _target_pct"


@case
def test_commute_target_capped_disabled_and_nodata():
    ca, _ = build({C.CA_BATTERY_KWH: 80, C.CA_TARGET_PCT: 80, C.CA_COMMUTE_ENABLED: True}, {})
    now = dt_util.utcnow().timestamp()
    ca._coordinator().data["charge_log"] = [{"start": now - 86400, "wh": 70000}]  # 70 kWh/day
    assert ca._commute_target() == 80.0, "huge use clamps to the configured cap"
    ca._opts[C.CA_COMMUTE_ENABLED] = False
    assert ca._target_pct() == 80.0, "disabled → fixed target"
    ca2, _ = build({C.CA_BATTERY_KWH: 80, C.CA_TARGET_PCT: 80, C.CA_COMMUTE_ENABLED: True}, {})
    ca2._coordinator().data["charge_log"] = []
    assert ca2._commute_target() is None and ca2._target_pct() == 80, "no data → fixed target"


class _HistState:
    """Minimal recorder-State stand-in: .state + .last_changed (datetime)."""
    def __init__(self, value, days_ago):
        from datetime import timedelta
        self.state = value
        self.last_changed = dt_util.utcnow() - timedelta(days=days_ago)


def _hist(pairs):
    # pairs: list of (days_ago, value), passed newest-first for readability;
    # the learner expects oldest-first, so reverse.
    return [_HistState(str(v), d) for d, v in sorted(pairs, key=lambda p: -p[0])]


@case
def test_commute_odometer_source_km_per_day():
    ca, _ = build({C.CA_COMMUTE_ENABLED: True}, {})
    # 1000 -> 1500 km across a 5-day span = 100 km/day
    km = ca._daily_km_from_states(_hist([(5, 1000), (3, 1200), (0, 1500)]))
    assert km is not None and 95 < km < 105, f"~100 km/day expected, got {km}"
    assert ca._daily_km_from_states([]) is None, "no history → None"
    # non-increasing / single point → None
    assert ca._daily_km_from_states(_hist([(2, 500), (0, 500)])) is None


@case
def test_commute_soc_source_drop_per_day():
    ca, _ = build({C.CA_COMMUTE_ENABLED: True}, {})
    # 80->60 (drop 20), 60->90 (charge, ignored), 90->70 (20), 70->40 (30) = 70%
    # across a 4-day span => 17.5 %/day
    pct = ca._daily_soc_drop_from_states(_hist([(4, 80), (3, 60), (2, 90), (1, 70), (0, 40)]))
    assert pct is not None and 16 < pct < 19, f"~17.5 %/day expected, got {pct}"
    # only charging (monotonic up) → no consumption → None
    assert ca._daily_soc_drop_from_states(_hist([(2, 40), (1, 60), (0, 80)])) is None


@case
def test_commute_source_dispatch_charger_default():
    # default source is "charger" → uses the live charge-log, not the cache
    ca, _ = build({C.CA_COMMUTE_ENABLED: True}, {})
    now = dt_util.utcnow().timestamp()
    ca._coordinator().data["charge_log"] = [{"start": now - 86400, "wh": 14000}]
    assert ca._learn_source(ca._active_car()) == "charger"
    assert ca._avg_daily_use_kwh() is not None, "charger source computes live"
    # switch to a history source → sync path returns the (empty) cache, not charge-log
    ca._opts[C.CA_COMMUTE_SOURCE] = "odometer"
    assert ca._avg_daily_use_kwh() is None, "history source uses cache (None until refresh)"
    ca._learned_daily_kwh = {"_default": 12.0}   # active car's per-car cache key
    assert ca._avg_daily_use_kwh() == 12.0, "history source returns the cached value"


@case
def test_projected_soc_and_days_until_reserve():
    opts = {C.CA_MODE: C.MODE_TARGET, C.CA_BATTERY_KWH: 80, C.CA_TARGET_PCT: 80,
            C.CA_COMMUTE_ENABLED: True, C.CA_COMMUTE_RESERVE: 20,
            C.CA_SOC_ENTITY: "sensor.soc"}
    ca, _ = build(opts, {"sensor.soc": FakeState("70")})
    now = dt_util.utcnow().timestamp()
    # 16 kWh/day over ~1-day span on an 80 kWh pack → 20%/day
    ca._coordinator().data["charge_log"] = [{"start": now - 86400, "wh": 16000}]
    assert abs(ca._daily_use_pct() - 20.0) < 1.0, ca._daily_use_pct()
    proj = ca._projected_soc_after_days(1.0)              # 70 − 20 = 50
    assert proj is not None and 49 < proj < 51, proj
    days = ca._days_until_reserve()                        # (70 − 20) / 20 = 2.5
    assert days is not None and 2.3 < days < 2.7, days


@case
def test_projected_soc_floors_and_none_without_inputs():
    # SOC below a full day's use floors at 0, never negative
    ca, _ = build({C.CA_BATTERY_KWH: 80, C.CA_SOC_ENTITY: "sensor.soc",
                   C.CA_COMMUTE_ENABLED: True}, {"sensor.soc": FakeState("5")})
    now = dt_util.utcnow().timestamp()
    ca._coordinator().data["charge_log"] = [{"start": now - 86400, "wh": 16000}]  # 20%/day
    assert ca._projected_soc_after_days(1.0) == 0.0, "floors at 0"
    # no SOC entity → None
    ca2, _ = build({C.CA_BATTERY_KWH: 80, C.CA_COMMUTE_ENABLED: True}, {})
    ca2._coordinator().data["charge_log"] = [{"start": now - 86400, "wh": 16000}]
    assert ca2._projected_soc_after_days(1.0) is None
    assert ca2._days_until_reserve() is None


@case
def test_multicar_active_resolution_and_per_car_targets():
    # Two cars: BYD (80 kWh, target 80) and Tesla (75 kWh, target 90), each on
    # the SOC source so their per-car cache drives a distinct commute target.
    cars = [
        {C.CA_CAR_NAME: "BYD", C.CA_SOC_ENTITY: "sensor.byd_soc",
         C.CA_BATTERY_KWH: 80, C.CA_TARGET_PCT: 80, C.CA_COMMUTE_ENABLED: True,
         C.CA_COMMUTE_SOURCE: "soc", C.CA_COMMUTE_RESERVE: 20,
         C.CA_COMMUTE_MARGIN: 10, C.CA_COMMUTE_COVER_DAYS: 1},
        {C.CA_CAR_NAME: "Tesla", C.CA_SOC_ENTITY: "sensor.tesla_soc",
         C.CA_BATTERY_KWH: 75, C.CA_TARGET_PCT: 90, C.CA_COMMUTE_ENABLED: True,
         C.CA_COMMUTE_SOURCE: "soc", C.CA_COMMUTE_RESERVE: 30,
         C.CA_COMMUTE_MARGIN: 5, C.CA_COMMUTE_COVER_DAYS: 1},
    ]
    states = {"sensor.byd_soc": FakeState("60"), "sensor.tesla_soc": FakeState("70")}
    ca, _ = build({C.CA_CARS: cars}, states)
    # per-car learned cache (kWh/day): BYD 16 (=20%/80), Tesla 15 (=20%/75)
    ca._learned_daily_kwh = {"BYD": 16.0, "Tesla": 15.0}

    byd, tesla = ca._cars()[0], ca._cars()[1]
    assert abs(ca._daily_use_pct(byd) - 20.0) < 0.1
    assert abs(ca._daily_use_pct(tesla) - 20.0) < 0.1
    # commute target = reserve + use×cover + margin, capped at the car's target
    assert abs(ca._commute_target(byd) - 50.0) < 0.1, "BYD 20+20+10"      # cap 80
    assert abs(ca._commute_target(tesla) - 55.0) < 0.1, "Tesla 30+20+5"   # cap 90
    # projected SOC reads each car's own SOC entity
    assert abs(ca._projected_soc_after_days(1.0, byd) - 40.0) < 0.1       # 60−20
    assert abs(ca._projected_soc_after_days(1.0, tesla) - 50.0) < 0.1     # 70−20

    # active car defaults to the first; CA_ACTIVE_CAR picks the other
    assert ca._active_car().get(C.CA_CAR_NAME) == "BYD"
    assert abs(ca._target_pct() - 50.0) < 0.1, "engine target = active (BYD)"
    ca._opts[C.CA_ACTIVE_CAR] = "Tesla"
    assert ca._active_car().get(C.CA_CAR_NAME) == "Tesla"
    assert abs(ca._target_pct() - 55.0) < 0.1, "engine target follows active car"


@case
def test_single_car_legacy_unchanged():
    # No CA_CARS → one legacy car ({}); reads fall straight through to top-level
    ca, _ = build({C.CA_BATTERY_KWH: 80, C.CA_TARGET_PCT: 80,
                   C.CA_SOC_ENTITY: "sensor.soc", C.CA_COMMUTE_ENABLED: True},
                  {"sensor.soc": FakeState("50")})
    assert ca._cars() == [{}] and ca._active_car() == {}
    now = dt_util.utcnow().timestamp()
    ca._coordinator().data["charge_log"] = [{"start": now - 86400, "wh": 16000}]
    assert ca._avg_daily_use_kwh() is not None, "legacy single-car still computes"
    assert ca._target_pct() <= 80.0


@case
def test_identity_guess_most_urgent_and_override():
    cars = [
        {C.CA_CAR_NAME: "BYD", C.CA_SOC_ENTITY: "sensor.byd", C.CA_BATTERY_KWH: 80,
         C.CA_COMMUTE_RESERVE: 20},
        {C.CA_CAR_NAME: "Tesla", C.CA_SOC_ENTITY: "sensor.tesla", C.CA_BATTERY_KWH: 75,
         C.CA_COMMUTE_RESERVE: 20},
    ]
    # BYD low (25%), Tesla high (80%) → BYD is the more urgent guess
    states = {"sensor.byd": FakeState("25"), "sensor.tesla": FakeState("80")}
    ca, _ = build({C.CA_CARS: cars}, states)
    assert ca._guess_active_car() == "BYD", "lowest SOC = most urgent guess"
    # confirm flips the active car, and _active_car() honours the override
    ca._set_active_car("Tesla", "user confirmed")
    assert ca._active_car().get(C.CA_CAR_NAME) == "Tesla"
    assert ca.active_vehicle_name() == "Tesla"
    # a bad name is ignored (stays on Tesla)
    ca._set_active_car("Ghost", "bogus")
    assert ca._active_car().get(C.CA_CAR_NAME) == "Tesla"


@case
def test_identity_soc_rise_wins_over_urgency():
    cars = [
        {C.CA_CAR_NAME: "BYD", C.CA_SOC_ENTITY: "sensor.byd", C.CA_BATTERY_KWH: 80},
        {C.CA_CAR_NAME: "Tesla", C.CA_SOC_ENTITY: "sensor.tesla", C.CA_BATTERY_KWH: 75},
    ]
    # BYD is lower (would be the urgency guess) but Tesla's SOC has RISEN since
    # plug-in → Tesla is the one actually charging, so it wins.
    states = {"sensor.byd": FakeState("30"), "sensor.tesla": FakeState("63")}
    ca, _ = build({C.CA_CARS: cars}, states)
    ca._plug_soc = {"BYD": 30.0, "Tesla": 60.0}     # Tesla +3, BYD +0
    assert ca._guess_active_car() == "Tesla", "SOC-rise beats urgency"


@case
def test_identity_single_car_is_noop():
    ca, _ = build({C.CA_BATTERY_KWH: 80, C.CA_SOC_ENTITY: "sensor.soc"},
                  {"sensor.soc": FakeState("50")})
    assert ca.active_vehicle_name() is None, "single-car: no identity"
    ca._plugged_was = False
    ca._maybe_identity()   # must not raise / prompt for a single car
    assert ca.active_vehicle_name() is None


@case
def test_recommended_plug_in_ranks_most_urgent():
    cars = [
        {C.CA_CAR_NAME: "BYD", C.CA_SOC_ENTITY: "sensor.byd", C.CA_BATTERY_KWH: 80,
         C.CA_TARGET_PCT: 80},
        {C.CA_CAR_NAME: "Tesla", C.CA_SOC_ENTITY: "sensor.tesla", C.CA_BATTERY_KWH: 75,
         C.CA_TARGET_PCT: 80},
    ]
    # Nothing on the cable → all cars are candidates. BYD 30% (deficit 50),
    # Tesla 70% (deficit 10) → recommend BYD (biggest deficit / most urgent).
    states = {"sensor.byd": FakeState("30"), "sensor.tesla": FakeState("70")}
    ca, _ = build({C.CA_CARS: cars}, states)
    ca._plugged_in = lambda: False
    assert ca.recommended_plug_in() == "BYD", "biggest deficit / most urgent first"
    detail = ca.recommended_plug_in_detail()
    assert detail["ranked"][0]["name"] == "BYD"
    assert detail["reason"] and "BYD" in detail["reason"]


@case
def test_recommended_plug_in_skips_satisfied_and_active():
    cars = [
        {C.CA_CAR_NAME: "BYD", C.CA_SOC_ENTITY: "sensor.byd", C.CA_BATTERY_KWH: 80, C.CA_TARGET_PCT: 80},
        {C.CA_CAR_NAME: "Tesla", C.CA_SOC_ENTITY: "sensor.tesla", C.CA_BATTERY_KWH: 75, C.CA_TARGET_PCT: 80},
    ]
    # Both already at/above target → nobody needs charge → None.
    ca, _ = build({C.CA_CARS: cars}, {"sensor.byd": FakeState("85"), "sensor.tesla": FakeState("90")})
    ca._plugged_in = lambda: False
    assert ca.recommended_plug_in() is None, "all satisfied → no recommendation"
    # BYD needs charge, Tesla satisfied → recommend BYD.
    ca2, _ = build({C.CA_CARS: cars}, {"sensor.byd": FakeState("40"), "sensor.tesla": FakeState("90")})
    ca2._plugged_in = lambda: False
    assert ca2.recommended_plug_in() == "BYD"
    # Active car (BYD) is on the cable → recommend the NEXT needing car (Tesla).
    ca3, _ = build({C.CA_CARS: cars}, {"sensor.byd": FakeState("30"), "sensor.tesla": FakeState("60")})
    ca3._plugged_in = lambda: True          # active defaults to first = BYD
    assert ca3.recommended_plug_in() == "Tesla", "exclude the car already plugged in"
    # single car → never recommends
    ca4, _ = build({C.CA_BATTERY_KWH: 80, C.CA_SOC_ENTITY: "sensor.b", C.CA_TARGET_PCT: 80},
                   {"sensor.b": FakeState("20")})
    assert ca4.recommended_plug_in() is None


@case
def test_feasibility_flags_when_window_too_short():
    cars = [
        {C.CA_CAR_NAME: "BYD", C.CA_SOC_ENTITY: "sensor.byd", C.CA_BATTERY_KWH: 80,
         C.CA_TARGET_PCT: 80, C.CA_CHARGE_POWER_KW: 7.0},
        {C.CA_CAR_NAME: "Tesla", C.CA_SOC_ENTITY: "sensor.tesla", C.CA_BATTERY_KWH: 75,
         C.CA_TARGET_PCT: 80, C.CA_CHARGE_POWER_KW: 7.0},
    ]
    # BYD 30→80 = 40 kWh ≈ 5.7h; Tesla 40→80 = 30 kWh ≈ 4.3h → ~10h total.
    # Cheap window 00:00–06:00 = 6h → can't do both.
    opts = {C.CA_CARS: cars, C.CA_WINDOW_ENABLED: True,
            C.CA_WINDOW_START: "00:00", C.CA_WINDOW_END: "06:00"}
    states = {"sensor.byd": FakeState("30"), "sensor.tesla": FakeState("40")}
    ca, _ = build(opts, states)
    f = ca._feasibility()
    assert f["available_hours"] == 6.0, f
    assert f["needed_hours"] > 6.0 and f["feasible"] is False, f
    assert f["feasibility_note"] and "prioritising" in f["feasibility_note"]
    # Loosen: only BYD needs a small top-up → fits in 6h → feasible
    ca2, _ = build(opts, {"sensor.byd": FakeState("75"), "sensor.tesla": FakeState("90")})
    f2 = ca2._feasibility()
    assert f2["feasible"] is True and f2["feasibility_note"] is None, f2


@case
def test_unknown_car_policy_targets():
    cars = [
        {C.CA_CAR_NAME: "BYD", C.CA_SOC_ENTITY: "sensor.byd", C.CA_BATTERY_KWH: 80, C.CA_TARGET_PCT: 90},
        {C.CA_CAR_NAME: "Tesla", C.CA_SOC_ENTITY: "sensor.tesla", C.CA_BATTERY_KWH: 75, C.CA_TARGET_PCT: 60},
    ]
    states = {"sensor.byd": FakeState("50"), "sensor.tesla": FakeState("50")}
    # Conservative (default): while the plugged car is a guess, cap at the lowest
    # target (60) — never over-charge an unknown car. Confirmed → full target (90).
    ca, _ = build({C.CA_CARS: cars}, states)
    ca._identity_confirmed = False
    assert ca._uncertain() is True
    assert ca._target_pct() == 60.0, "conservative caps at the lowest target"
    ca._identity_confirmed = True
    assert ca._target_pct() == 90.0, "confirmed → active car's full target"
    # Ask: target = current SOC → no deficit → won't auto-start until confirmed.
    ca2, _ = build({C.CA_CARS: cars, C.CA_UNKNOWN_CAR: "ask"}, states)
    ca2._identity_confirmed = False
    assert ca2._target_pct() == 50.0, "ask holds at current SOC until confirmed"
    # Assume: act on the best guess immediately (active car's full target).
    ca3, _ = build({C.CA_CARS: cars, C.CA_UNKNOWN_CAR: "assume_last"}, states)
    ca3._identity_confirmed = False
    assert ca3._target_pct() == 90.0, "assume → the guessed car's target"


def main():
    if not _HA_OK:
        return
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
