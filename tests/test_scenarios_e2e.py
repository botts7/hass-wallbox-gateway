"""End-to-end scenario matrix for the Charge Assistant controller.

Proves every USER-SELECTABLE mode behaves soundly across its key conditions.
Same pure-logic harness as tests/test_controller_decisions.py (no pytest, no
pytest-homeassistant): HA must be importable, else the whole module self-skips.

Entry points exercised (learned from charge_assistant.py):
  * strategy dispatch      — ca_config.strategy_of(opts) → MODE_* ; _ACTING set
  * MODE_OFF / MODE_REMINDER — no acting strategy (reminder is a notify LAYER)
  * MODE_TARGET            — _eval_target / _eval_target_windowed
  * MODE_SOLAR             — _eval_solar
  * MODE_SMART_SOLAR       — _eval_smart_solar
  * window policy          — charge_window.evaluate (pure) + _window_decision
  * commute hierarchy      — _commute_enabled / _commute_target vs _target_pct

Every acting assertion is on the recorded `calls` list (each _set_charging
True/False), the same contract build() gives test_controller_decisions.

Run:  py tests/test_scenarios_e2e.py   (or via tests/run_all.py)
"""

import os
import sys
from datetime import timedelta

# custom_components on path so the package's relative imports resolve and the
# stdlib `select` isn't shadowed by the integration's select.py platform.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

try:
    import wallbox_gateway.charge_assistant as ca_mod
    from wallbox_gateway import const as C
    from wallbox_gateway import ca_config
    from wallbox_gateway import charge_window
    from homeassistant.util import dt as dt_util
    _HA_OK = True
except Exception as e:  # pragma: no cover - environment without HA
    print(f"--- test_scenarios_e2e: SKIPPED (HA not importable: {e})")
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
            coro.close()      # we assert on _set_charging, not the BLE / notify coro
        except Exception:
            pass


class FakeEntry:
    def __init__(self, opts):
        self.entry_id = "e1"
        self.data = {}
        self.options = {C.CA_KEY: opts}


def build(opts, states):
    """Return (ca, calls) — calls records every _set_charging(True/False).

    Matches test_controller_decisions.build so scenarios read identically:
    plugged-in by default (override ca._plugged_in for unplugged cases),
    owner == integration (so _may_control allows), _set_charging stubbed.
    """
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


def _hhmm(delta_h):
    """Local HH:MM `delta_h` hours from now (for window/departure tests)."""
    return (dt_util.now() + timedelta(hours=delta_h)).strftime("%H:%M")


# A helper to patch async_call_later for the paths that schedule a finish/grace
# timer (target stop-at-cap). Returns a restore callable.
def _patch_call_later(sched):
    orig = ca_mod.async_call_later
    ca_mod.async_call_later = lambda hass, delay, cb: (
        sched.update(delay=delay, cb=cb) or (lambda: None))
    return orig


# ════════════════════════════════════════════════════════════════════
# MODE OFF — the acting layer is NEVER engaged, under any condition.
# ════════════════════════════════════════════════════════════════════
@case
def test_off_is_not_an_acting_strategy():
    opts = {C.CA_MODE: C.MODE_OFF}
    assert ca_config.strategy_of(opts) == C.MODE_OFF
    assert C.MODE_OFF not in ca_mod._ACTING
    ca, calls = build(opts, {})
    assert ca._should_control() is False, "off must never take charge control"
    assert ca.next_start_estimate()["state"] == "off"
    assert ca._plan_clause() is None
    assert calls == []


@case
def test_off_ignores_plugged_low_soc_surplus_in_window():
    # Every condition that would make an ACTING mode charge — off still won't
    # engage: it wires no eval loop and never becomes a controller.
    opts = {C.CA_MODE: C.MODE_OFF, C.CA_SOC_ENTITY: "sensor.soc",
            C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True,
            C.CA_SURPLUS_SOURCE: "entity", C.CA_SURPLUS_ENTITY: "sensor.surplus",
            C.CA_SURPLUS_START: 1.0, C.CA_WINDOW_ENABLED: True,
            C.CA_WINDOW_START: _hhmm(-1), C.CA_WINDOW_END: _hhmm(1)}
    states = {"sensor.soc": FakeState("20"), "sensor.surplus": FakeState("5000")}
    ca, calls = build(opts, states)
    assert ca._should_control() is False
    assert ca_config.strategy_of(opts) == C.MODE_OFF
    assert calls == [], "off drives no charging regardless of conditions"


# ════════════════════════════════════════════════════════════════════
# MODE REMINDER — a notify LAYER, not a charging strategy.
# ════════════════════════════════════════════════════════════════════
@case
def test_reminder_resolves_to_off_charging_strategy():
    # Legacy flat reminder mode is a LAYER: the acting strategy is 'off'.
    opts = {C.CA_MODE: C.MODE_REMINDER, C.CA_TRIGGERS: [C.TRIG_NIGHTLY],
            C.CA_NOTIFY_SERVICE: "notify.me"}
    assert ca_config.strategy_of(opts) == C.MODE_OFF
    assert ca_config.reminder_enabled(opts) is True
    ca, calls = build(opts, {})
    assert ca._should_control() is False
    assert calls == []


@case
def test_reminder_layer_notifies_but_never_charges():
    # The reminder paths (_eval_solar_remind → _maybe_remind) may fire a
    # notification but must NEVER touch _set_charging.
    opts = {C.CA_SURPLUS_SOURCE: "entity", C.CA_SURPLUS_ENTITY: "sensor.surplus",
            C.CA_NOTIFY_SERVICE: "notify.me"}
    ca, calls = build(opts, {"sensor.surplus": FakeState("3.0")})
    ca._plugged_in = lambda: False          # unplugged → the nudge is allowed
    ca._rem = {C.CA_SOLAR_REMIND_KW: 1.4, C.CA_NOTIFY_SERVICE: "notify.me"}
    ca._eval_solar_remind()                 # surplus 3.0 >= 1.4 → nudge fires
    ca._maybe_remind("nightly")             # direct layer entry point
    assert calls == [], "reminder layer must not drive charging"


# ════════════════════════════════════════════════════════════════════
# MODE TARGET (Smart charge) — _eval_target
# ════════════════════════════════════════════════════════════════════
def _target_opts(**over):
    o = {C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
         C.CA_TARGET_PCT: 80, C.CA_TARGET_AUTOSTART: True}
    o.update(over)
    return o


@case
def test_target_autostart_starts_below_target():
    ca, calls = build(_target_opts(), {"sensor.soc": FakeState("50")})
    ca._eval_target()
    assert calls == [True], f"plugged + below target should start, got {calls}"


@case
def test_target_autostart_no_start_at_or_above_target():
    # SOC == target and SOC > target: idle → nothing to start, nothing to stop.
    for soc in ("80", "95"):
        ca, calls = build(_target_opts(), {"sensor.soc": FakeState(soc)})
        ca._eval_target()
        assert calls == [], f"at/above target should not start (soc={soc}), got {calls}"


@case
def test_target_autostart_no_start_unknown_soc():
    # SOC sensor dropped out → must NOT start (never charge on an unknown SOC).
    ca, calls = build(_target_opts(), {"sensor.soc": FakeState("unknown")})
    ca._eval_target()
    assert calls == [], f"unknown SOC must not start, got {calls}"
    # Also with the entity entirely absent.
    ca2, calls2 = build(_target_opts(), {})
    ca2._eval_target()
    assert calls2 == [], f"missing SOC must not start, got {calls2}"


@case
def test_target_autostart_blocked_above_price_cap():
    opts = _target_opts(price_entity="sensor.price", price_cap=0.40)
    states = {"sensor.soc": FakeState("50"), "sensor.price": FakeState("0.55")}
    ca, calls = build(opts, states)
    ca._eval_target()
    assert calls == [], f"above price cap should wait, got {calls}"


@case
def test_target_autostart_allowed_below_price_cap():
    opts = _target_opts(price_entity="sensor.price", price_cap=0.40)
    states = {"sensor.soc": FakeState("50"), "sensor.price": FakeState("0.20")}
    ca, calls = build(opts, states)
    ca._eval_target()
    assert calls == [True], f"below price cap should start, got {calls}"


@case
def test_target_autostart_off_never_auto_starts():
    # Manual smart charge: waits for a manual Start, never auto-starts.
    ca, calls = build(_target_opts(target_autostart=False),
                      {"sensor.soc": FakeState("40")})
    ca._eval_target()
    assert calls == [], f"autostart OFF must not auto-start, got {calls}"


@case
def test_target_no_start_when_unplugged():
    ca, calls = build(_target_opts(), {"sensor.soc": FakeState("40")})
    ca._plugged_in = lambda: False
    ca._eval_target()
    assert calls == [], f"unplugged must not start, got {calls}"


@case
def test_target_stops_when_charging_at_target():
    sched = {}
    orig = _patch_call_later(sched)
    try:
        ca, calls = build(_target_opts(), {"sensor.soc": FakeState("85")})
        ca._is_charging = lambda: True
        ca._we_started = True
        ca._eval_target()
        assert calls == [False], f"at target while charging should stop, got {calls}"
        assert ca._reached_target is True
    finally:
        ca_mod.async_call_later = orig


@case
def test_target_departure_jit_waits_when_too_early():
    # Departure is 10h away, only a tiny top-up needed → far too early → wait.
    opts = _target_opts(target_autostart=False, departure_time=_hhmm(10),
                        battery_kwh=60, charge_power_kw=7.4)
    ca, calls = build(opts, {"sensor.soc": FakeState("78")})
    ca._eval_target()
    assert calls == [], f"JIT far before departure should wait, got {calls}"


@case
def test_target_departure_jit_starts_when_late():
    # Departure imminent (~12 min) and a real gap → must start now to make it.
    opts = _target_opts(target_autostart=False, departure_time=_hhmm(0.2),
                        battery_kwh=80, charge_power_kw=6.8)
    ca, calls = build(opts, {"sensor.soc": FakeState("55")})
    ca._eval_target()
    assert calls == [True], f"JIT at the deadline should start, got {calls}"


# ════════════════════════════════════════════════════════════════════
# MODE SOLAR — _eval_solar (surplus-follow with hysteresis + debounce)
# ════════════════════════════════════════════════════════════════════
def _solar_opts(**over):
    o = {C.CA_MODE: C.MODE_SOLAR, C.CA_SURPLUS_SOURCE: "entity",
         C.CA_SURPLUS_ENTITY: "sensor.surplus", C.CA_SURPLUS_START: 1.4,
         C.CA_SURPLUS_STOP: 0, C.CA_SURPLUS_DEBOUNCE: 0}
    o.update(over)
    return o


@case
def test_solar_starts_when_surplus_at_or_above_start():
    ca, calls = build(_solar_opts(), {"sensor.surplus": FakeState("3.0")})
    ca._eval_solar()
    assert calls == [True], f"surplus >= start should charge, got {calls}"


@case
def test_solar_stops_when_surplus_drops_to_stop():
    ca, calls = build(_solar_opts(), {"sensor.surplus": FakeState("0")})
    ca._is_charging = lambda: True
    ca._we_started = True
    ca._eval_solar()
    assert calls == [False], f"surplus <= stop should pause, got {calls}"


@case
def test_solar_never_grid_starts_on_zero_surplus():
    # Idle + zero surplus → the whole point of solar mode: no grid start.
    ca, calls = build(_solar_opts(), {"sensor.surplus": FakeState("0")})
    ca._eval_solar()
    assert calls == [], f"zero surplus must never start, got {calls}"


@case
def test_solar_hysteresis_band_holds_state():
    # Surplus between stop (0.5) and start (2.0) → hold; neither start nor stop.
    ca, calls = build(_solar_opts(surplus_start=2.0, surplus_stop=0.5),
                      {"sensor.surplus": FakeState("1.0")})
    ca._eval_solar()
    assert calls == [], f"hysteresis band should hold, got {calls}"


@case
def test_solar_debounce_waits_before_starting():
    # Surplus is high but the debounce hasn't elapsed on this first tick → arm
    # the timer, do NOT start yet.
    ca, calls = build(_solar_opts(surplus_start=1.0, surplus_debounce_min=5),
                      {"sensor.surplus": FakeState("3.0")})
    ca._eval_solar()
    assert calls == [], f"debounce should defer the start, got {calls}"
    assert ca._surplus_since is not None, "debounce timer should be armed"


@case
def test_solar_surplus_unknown_does_nothing():
    ca, calls = build(_solar_opts(), {"sensor.surplus": FakeState("unavailable")})
    ca._eval_solar()
    assert calls == [], f"unknown surplus must be a no-op, got {calls}"


# ════════════════════════════════════════════════════════════════════
# MODE SMART_SOLAR — _eval_smart_solar (solar-first, grid bounded by window)
# ════════════════════════════════════════════════════════════════════
def _smart_opts(**over):
    # debounce 0 so these decision-tests act instantly; the hysteresis+debounce
    # timing itself is covered by test_smart_solar_solar_hysteresis_debounce.
    o = {C.CA_MODE: C.MODE_SMART_SOLAR, C.CA_SOC_ENTITY: "sensor.soc",
         C.CA_TARGET_PCT: 80, C.CA_SURPLUS_SOURCE: "entity",
         C.CA_SURPLUS_ENTITY: "sensor.surplus", C.CA_SURPLUS_START: 1.0,
         C.CA_SURPLUS_DEBOUNCE: 0}
    o.update(over)
    return o


@case
def test_smart_solar_starts_from_solar_above_target():
    # Free solar keeps filling past the SOC target (target only caps GRID).
    ca, calls = build(_smart_opts(),
                      {"sensor.soc": FakeState("85"), "sensor.surplus": FakeState("2000")})
    ca._eval_smart_solar()
    assert calls == [True], f"solar should charge above target, got {calls}"


@case
def test_smart_solar_grid_topup_only_below_target_in_window():
    # No surplus, SOC below target, INSIDE the cheap window → grid top-up allowed.
    opts = _smart_opts(window_enabled=True,
                       window_start=_hhmm(-1), window_end=_hhmm(1))
    ca, calls = build(opts,
                      {"sensor.soc": FakeState("50"), "sensor.surplus": FakeState("0")})
    ca._eval_smart_solar()
    assert calls == [True], f"in-window grid top-up should start, got {calls}"


@case
def test_smart_solar_grid_blocked_outside_window():
    opts = _smart_opts(window_enabled=True,
                       window_start=_hhmm(2), window_end=_hhmm(3))
    ca, calls = build(opts,
                      {"sensor.soc": FakeState("50"), "sensor.surplus": FakeState("0")})
    ca._eval_smart_solar()
    assert calls == [], f"outside window, no surplus should wait, got {calls}"


@case
def test_smart_solar_no_grid_start_on_unknown_soc():
    # REGRESSION: an unknown SOC must NOT be treated as "below target" — even
    # inside the cheap window — or a full battery could grid-charge at peak the
    # moment the SOC sensor blips.
    opts = _smart_opts(window_enabled=True,
                       window_start=_hhmm(-1), window_end=_hhmm(1))
    ca, calls = build(opts,
                      {"sensor.soc": FakeState("unknown"), "sensor.surplus": FakeState("0")})
    ca._eval_smart_solar()
    assert calls == [], f"unknown SOC must not grid-start, got {calls}"


@case
def test_smart_solar_overrun_does_not_initiate_fresh_grid_outside_window():
    # REGRESSION: overrun only EXTENDS an already-running charge. Idle + outside
    # window + no surplus must NOT start a fresh grid charge even with overrun on.
    opts = _smart_opts(window_enabled=True, window_overrun=True,
                       window_start=_hhmm(2), window_end=_hhmm(3))
    ca, calls = build(opts,
                      {"sensor.soc": FakeState("50"), "sensor.surplus": FakeState("0")})
    ca._is_charging = lambda: False        # not already charging
    ca._eval_smart_solar()
    assert calls == [], f"overrun must not initiate a fresh grid charge, got {calls}"


@case
def test_smart_solar_stops_at_target_with_no_solar():
    ca, calls = build(_smart_opts(),
                      {"sensor.soc": FakeState("85"), "sensor.surplus": FakeState("0")})
    ca._is_charging = lambda: True
    ca._we_started = True
    ca._eval_smart_solar()
    assert calls == [False], f"target reached + no solar should stop, got {calls}"


@case
def test_smart_solar_grid_blocked_above_price_cap_in_window():
    # In-window grid top-up still respects the hard price cap.
    opts = _smart_opts(window_enabled=True,
                       window_start=_hhmm(-1), window_end=_hhmm(1),
                       price_entity="sensor.price", price_cap=0.30)
    ca, calls = build(opts, {"sensor.soc": FakeState("50"),
                             "sensor.surplus": FakeState("0"),
                             "sensor.price": FakeState("0.60")})
    ca._eval_smart_solar()
    assert calls == [], f"above price cap should block grid top-up, got {calls}"
    # Drop the price under the cap → grid top-up proceeds.
    ca2, calls2 = build(opts, {"sensor.soc": FakeState("50"),
                               "sensor.surplus": FakeState("0"),
                               "sensor.price": FakeState("0.10")})
    ca2._eval_smart_solar()
    assert calls2 == [True], f"below price cap should allow grid top-up, got {calls2}"


# ════════════════════════════════════════════════════════════════════
# WINDOW policy cross-cut — pure charge_window.evaluate
# ════════════════════════════════════════════════════════════════════
@case
def test_window_no_window_always_allows():
    d = charge_window.evaluate(600, start=None, end=None)
    assert d["allow_charge"] is True and d["reason"] == "no_window"


@case
def test_window_in_window_allows_outside_blocks():
    # Window 01:00–06:00 (60–360). 03:00 inside → allow; 12:00 outside → block.
    inside = charge_window.evaluate(180, start="01:00", end="06:00")
    assert inside["in_window"] is True and inside["allow_charge"] is True
    outside = charge_window.evaluate(720, start="01:00", end="06:00")
    assert outside["in_window"] is False and outside["allow_charge"] is False
    assert outside["reason"] == "outside_window"


@case
def test_window_overrun_extends_only_already_charging():
    # Outside window, below target, overrun ON.
    idle = charge_window.evaluate(720, start="01:00", end="06:00",
                                  overrun=True, already_charging=False)
    assert idle["allow_charge"] is False, "overrun must not INITIATE"
    running = charge_window.evaluate(720, start="01:00", end="06:00",
                                     overrun=True, already_charging=True)
    assert running["allow_charge"] is True and running["reason"] == "overrun_to_target"
    assert running["cost_warn"] is True


@case
def test_window_prestart_initiates_only_on_deadline():
    # Outside window, prestart ON. Plenty of time (dep far) → still blocked;
    # deadline pressure (mins_to_dep <= mins_needed) → pre-start initiates.
    early = charge_window.evaluate(720, start="01:00", end="06:00",
                                   prestart=True, minutes_to_departure=600,
                                   minutes_needed=120)
    assert early["allow_charge"] is False, "prestart must wait while there's time"
    due = charge_window.evaluate(720, start="01:00", end="06:00",
                                 prestart=True, minutes_to_departure=90,
                                 minutes_needed=120)
    assert due["allow_charge"] is True and due["reason"] == "prestart_for_departure"
    assert due["cost_warn"] is True


@case
def test_window_target_met_never_charges_outside():
    d = charge_window.evaluate(720, start="01:00", end="06:00",
                               overrun=True, already_charging=True, target_met=True)
    assert d["allow_charge"] is False and d["reason"] == "target_met"


@case
def test_window_integrated_target_stops_outside_no_overrun():
    # Integrated: charging but now OUTSIDE the window, overrun OFF → stop.
    sched = {}
    orig = _patch_call_later(sched)
    try:
        opts = _target_opts(window_enabled=True, window_overrun=False,
                            window_start=_hhmm(2), window_end=_hhmm(3))
        ca, calls = build(opts, {"sensor.soc": FakeState("60")})
        ca._is_charging = lambda: True
        ca._we_started = True
        ca._eval_target()
        assert calls == [False], f"outside window w/o overrun should stop, got {calls}"
    finally:
        ca_mod.async_call_later = orig


# ════════════════════════════════════════════════════════════════════
# COMMUTE hierarchy cross-cut — sensor can never disagree with enforcement
# ════════════════════════════════════════════════════════════════════
def _commute_opts(global_on, cars):
    return {C.CA_TARGET_PCT: 80, C.CA_COMMUTE_ENABLED: global_on,
            C.CA_COMMUTE_RESERVE: 40, C.CA_COMMUTE_MARGIN: 10,
            C.CA_COMMUTE_COVER_DAYS: 1, C.CA_CARS: cars}


@case
def test_commute_single_car_global_wins_over_leaked_percar_flag():
    # Single car carrying a leaked per-car commute_enabled:false must NOT shadow
    # the user's global commute:true. Sensor == enforcement.
    opts = _commute_opts(True, [{C.CA_CAR_NAME: "BYD", C.CA_COMMUTE_ENABLED: False}])
    ca, _ = build(opts, {})
    ca._daily_use_pct = lambda car: 1.0        # 40 + 1*1 + 10 = 51
    car = ca._active_car()
    assert ca._commute_enabled(car) is True
    assert ca._commute_target(car) == 51.0     # what the sensor shows
    assert ca._target_pct() == 51.0            # exactly what's enforced


@case
def test_commute_disabled_sensor_none_and_target_fixed():
    opts = _commute_opts(False, [{C.CA_CAR_NAME: "BYD"}])
    ca, _ = build(opts, {})
    ca._daily_use_pct = lambda car: 5.0
    car = ca._active_car()
    assert ca._commute_enabled(car) is False
    assert ca._commute_target(car) is None, "disabled → no phantom sensor value"
    assert ca._target_pct() == 80.0, "disabled → fixed target enforced"


@case
def test_commute_multicar_per_car_optout_honored():
    cars = [{C.CA_CAR_NAME: "A"}, {C.CA_CAR_NAME: "B", C.CA_COMMUTE_ENABLED: False}]
    ca, _ = build(_commute_opts(True, cars), {})
    ca._daily_use_pct = lambda car: 1.0
    a, b = ca._cars()[0], ca._cars()[1]
    assert ca._commute_enabled(a) is True and ca._commute_target(a) == 51.0
    assert ca._commute_enabled(b) is False and ca._commute_target(b) is None


@case
def test_commute_target_never_disagrees_with_enforcement_matrix():
    # Cross-check the invariant directly across enabled/disabled for the active
    # car: whenever commute is enabled, _target_pct == _commute_target (no trip);
    # whenever disabled, _commute_target is None and _target_pct is the fixed cap.
    for enabled in (True, False):
        opts = _commute_opts(enabled, [{C.CA_CAR_NAME: "BYD"}])
        ca, _ = build(opts, {})
        ca._daily_use_pct = lambda car: 2.0     # 40 + 2 + 10 = 52 when enabled
        car = ca._active_car()
        ct = ca._commute_target(car)
        if enabled:
            assert ct == 52.0 and ca._target_pct() == ct, "sensor must match enforcement"
        else:
            assert ct is None and ca._target_pct() == 80.0, "no phantom, fixed cap"


def main():
    if not _HA_OK:
        return
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
