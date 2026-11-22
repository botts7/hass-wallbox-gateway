"""Plan around tomorrow's solar: home_pattern, solar_outlook, overnight_plan.

Replays the weekend of 30-31 Oct 2026 at the test house: the car took about
27 kWh overnight at the cheap rate while the house then exported about 34 kWh
of spare solar, much of it while the car sat at home on Saturday.
Run:  py tests/test_solar_plan.py
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "custom_components", "wallbox_gateway"))
import home_pattern as HP  # noqa: E402
import solar_outlook as SO  # noqa: E402
import overnight_plan as OP  # noqa: E402

TZ = timezone(timedelta(hours=11))
CASES = []
def case(fn):
    CASES.append(fn); return fn


def _t(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ)


# ── home_pattern ────────────────────────────────────────────────────
def _history(weeks: int, plugged_fn, end: date) -> dict:
    """Synthetic history: for each day and half hour, plugged_fn(date, slot)."""
    days: dict = {}
    for i in range(weeks * 7):
        d = end - timedelta(days=i + 1)
        for s in range(HP.SLOTS):
            HP.record(days, _t(d, s // 2, 30 * (s % 2)), plugged_fn(d, s))
    return days


@case
def test_unknown_without_enough_history():
    today = date(2026, 11, 1)
    days = _history(1, lambda d, s: True, today)       # one week: 5 weekdays
    assert HP.probability(days, _t(date(2026, 11, 2), 10), today, min_days=14) is None


@case
def test_weekend_home_weekday_away():
    today = date(2026, 11, 1)
    # Home all weekend; on weekdays away 08:00-17:30.
    def plugged(d, s):
        return HP.is_weekend(d) or not (16 <= s < 35)
    days = _history(4, plugged, today)
    sat_noon = _t(date(2026, 11, 7), 12)
    tue_noon = _t(date(2026, 11, 3), 12)
    assert HP.probability(days, sat_noon, today) > 0.9
    assert HP.probability(days, tue_noon, today) < 0.1


@case
def test_recent_days_count_more():
    today = date(2026, 11, 1)
    # Weekday noons: home for the last 2 weeks, away the 4 weeks before.
    def plugged(d, s):
        return (today - d).days <= 14
    days = _history(6, plugged, today)
    p = HP.probability(days, _t(date(2026, 11, 3), 12), today)
    assert 0.5 < p < 1.0, p


@case
def test_prune_drops_old_and_future():
    days = {"2026-01-01": [0] * 48, "2026-10-31": [1] * 48, "2027-01-01": [1] * 48, "x": [1]}
    HP.prune(days, date(2026, 11, 1))
    assert list(days) == ["2026-10-31"]


@case
def test_seed_from_intervals_keeps_live_samples():
    d = date(2026, 10, 31)
    days: dict = {}
    HP.record(days, _t(d, 10), False)                   # a live sample wins
    changes = [(_t(d, 0), False), (_t(d, 9, 50), True), (_t(d, 13, 10), False)]
    n = HP.seed_from_intervals(days, changes, _t(d, 0), _t(d, 23, 59))
    row = days[d.isoformat()]
    assert row[HP.slot_of(_t(d, 10))] == 0              # live sample kept
    assert row[HP.slot_of(_t(d, 11))] == 1 and row[HP.slot_of(_t(d, 12, 30))] == 1
    assert row[HP.slot_of(_t(d, 14))] == 0 and n > 40


@case
def test_seed_counts_only_whole_half_hours():
    """17 Nov: unplugged at 8:31, so 8:30 to 9:00 is away, not home."""
    d = date(2026, 11, 17)
    days: dict = {}
    changes = [(_t(d, 0), True), (_t(d, 8, 31), False), (_t(d, 18, 39), True)]
    HP.seed_from_intervals(days, changes, _t(d, 0), _t(d, 23, 59))
    row = days[d.isoformat()]
    assert row[HP.slot_of(_t(d, 8))] == 1
    assert row[HP.slot_of(_t(d, 8, 30))] == 0             # left mid-slot
    assert row[HP.slot_of(_t(d, 18, 30))] == 0            # arrived mid-slot
    assert row[HP.slot_of(_t(d, 19))] == 1


@case
def test_seed_ignores_restart_blips_and_can_overwrite():
    d = date(2026, 11, 17)
    days = {d.isoformat(): [1] * HP.SLOTS}                 # old start-of-slot data
    changes = [(_t(d, 0), True), (_t(d, 7, 40), None), (_t(d, 7, 41), True),
               (_t(d, 8, 31), False)]
    HP.seed_from_intervals(days, changes, _t(d, 0), _t(d, 12), overwrite=True)
    row = days[d.isoformat()]
    assert row[HP.slot_of(_t(d, 7, 30))] == 1             # unavailable blip ignored
    assert row[HP.slot_of(_t(d, 8, 30))] == 0             # overwritten
    assert row[HP.slot_of(_t(d, 13))] == 1                # outside the seed: kept


@case
def test_set_slot_records_a_finished_half_hour():
    d = date(2026, 11, 17)
    days: dict = {}
    HP.set_slot(days, _t(d, 8, 30), False)
    HP.set_slot(days, _t(d, 8), True)
    row = days[d.isoformat()]
    assert row[HP.slot_of(_t(d, 8))] == 1 and row[HP.slot_of(_t(d, 8, 30))] == 0


# ── solar_outlook ───────────────────────────────────────────────────
@case
def test_two_planes_add_up():
    d = date(2026, 10, 31)
    a = {"wh_hours": {_t(d, 10).isoformat(): 1500, _t(d, 11).isoformat(): 2000}}
    b = {"wh_hours": {_t(d, 10).isoformat(): 500}}
    out = SO.from_wh_hours([a, b])
    assert out[_t(d, 10)] == 2.0 and out[_t(d, 11)] == 2.0


@case
def test_outlook_sensor_prefers_cautious_figure():
    d = date(2026, 10, 31)
    out = SO.from_hourly_attr([
        {"start": _t(d, 10).isoformat(), "kwh": 3.0, "kwh_low": 2.2},
        {"start": _t(d, 11).isoformat(), "kwh": 3.5},
        {"start": "bad", "kwh": 9},
    ])
    assert out == {_t(d, 10): 2.2, _t(d, 11): 3.5}


@case
def test_correction_needs_history_then_reads_low():
    # Forecast.Solar reads low at this house: actual about 1.3x to 3x.
    hist = {f"2026-10-{20+i:02d}": {"forecast": 10.0, "actual": a}
            for i, a in enumerate([13, 30, 14, 25, 13.5, 28, 15])}
    assert SO.learn_factor(dict(list(hist.items())[:4])) == 1.0   # too few days
    f = SO.learn_factor(hist)
    assert 1.3 <= f <= 1.6, f                           # 40th percentile, cautious


@case
def test_spare_caps_and_takes_baseline():
    h = _t(date(2026, 10, 31), 12)
    out = SO.spare({h: 4.0}, factor=3.0, baseline_kw=0.6, max_hour_kwh=8.2)
    assert abs(out[h] - 7.6) < 1e-9                     # 12 capped to 8.2, less 0.6


# ── overnight_plan ──────────────────────────────────────────────────
def _sat_spare():
    d = date(2026, 10, 31)
    return {_t(d, h): v for h, v in {9: 3.5, 10: 4.5, 11: 5.0, 12: 5.0, 13: 5.0,
                                       14: 4.5, 15: 3.5, 16: 2.0}.items()}


@case
def test_31_oct_home_saturday_leaves_most_to_solar():
    # 55% -> target 78%, 82.56 kWh, home 9:00-13:00 on Saturdays.
    home = lambda h: 0.9 if 9 <= h.hour < 13 else 0.2
    p = OP.plan(soc=55, target_pct=78, floor_pct=50, battery_kwh=82.56, efficiency=0.9,
                spare_by_hour=_sat_spare(), home_chance=home, threshold=0.7, charger_kw=7.0)
    assert p.need_kwh > 20                              # about 21 kWh from the grid before
    assert p.solar_kwh > 15                             # 4 good hours at home
    assert p.tonight_kwh < p.need_kwh / 3, p            # most of it left to solar
    assert p.target_pct >= 55
    assert "solar" in p.reason


@case
def test_floor_always_met_even_with_a_big_forecast():
    home = lambda h: 1.0
    p = OP.plan(soc=30, target_pct=80, floor_pct=50, battery_kwh=82.56, efficiency=0.9,
                spare_by_hour=_sat_spare(), home_chance=home, threshold=0.7, charger_kw=7.0)
    assert p.target_pct >= 50 and p.tonight_kwh >= p.floor_kwh


@case
def test_unknown_pattern_or_away_charges_as_before():
    for home in (lambda h: None, lambda h: 0.3):
        p = OP.plan(soc=55, target_pct=78, floor_pct=50, battery_kwh=82.56, efficiency=0.9,
                    spare_by_hour=_sat_spare(), home_chance=home, threshold=0.7, charger_kw=7.0)
        assert p.solar_kwh == 0 and p.tonight_kwh == p.need_kwh and p.target_pct == 78


@case
def test_aircon_reservation_is_left_alone():
    home = lambda h: 1.0
    spare = {_t(date(2026, 10, 31), 12): 5.0}
    full = OP.plan(soc=55, target_pct=78, floor_pct=50, battery_kwh=82.56, efficiency=1.0,
                   spare_by_hour=spare, home_chance=home, threshold=0.7, charger_kw=7.0)
    kept = OP.plan(soc=55, target_pct=78, floor_pct=50, battery_kwh=82.56, efficiency=1.0,
                   spare_by_hour=spare, home_chance=home, threshold=0.7, charger_kw=7.0,
                   reserve_kwh_by_hour={_t(date(2026, 10, 31), 12): 2.0})
    assert abs(full.solar_kwh - kept.solar_kwh - 2.0) < 0.11


@case
def test_charger_limit_caps_an_hour():
    home = lambda h: 1.0
    p = OP.plan(soc=0, target_pct=100, floor_pct=0, battery_kwh=82.56, efficiency=1.0,
                spare_by_hour={_t(date(2026, 10, 31), 12): 12.0}, home_chance=home,
                threshold=0.7, charger_kw=7.0)
    assert p.solar_kwh == 7.0


@case
def test_nothing_needed():
    p = OP.plan(soc=90, target_pct=78, floor_pct=50, battery_kwh=82.56, efficiency=0.9,
                spare_by_hour=_sat_spare(), home_chance=lambda h: 1.0, threshold=0.7, charger_kw=7.0)
    assert p.need_kwh == 0 and p.tonight_kwh == 0 and "No charge needed" in p.reason


# ── the Charge Assistant hook ───────────────────────────────────────
try:
    sys.path.insert(0, HERE)
    import test_controller_decisions as T   # reuse build() / FakeState
    _HA_OK = T._HA_OK
except Exception as e:  # pragma: no cover
    print(f"--- test_solar_plan (assistant part): SKIPPED ({e})")
    _HA_OK = False


def _planner(mode, plan_target, floor=50.0, fresh=True):
    """A real SolarPlanner with its plan filled in, no I/O."""
    from homeassistant.util import dt as dt_util
    import wallbox_gateway.solar_planner as SP
    pl = SP.SolarPlanner.__new__(SP.SolarPlanner)
    opts = {"solar_plan": mode, "solar_floor_pct": floor}
    pl._opts = lambda: opts
    pl._plan = OP.Plan(need_kwh=21, floor_kwh=0, solar_kwh=16, tonight_kwh=3,
                       target_pct=plan_target)
    pl._computed_at = dt_util.now() - (timedelta(0) if fresh else timedelta(hours=2))
    pl.result = {"tonight_kwh": 3}
    return pl


def _ca(target=78):
    C = T.C
    ca, _ = T.build({C.CA_MODE: C.MODE_TARGET, C.CA_TARGET_PCT: target,
                     C.CA_SOC_ENTITY: "sensor.soc"}, {"sensor.soc": T.FakeState("55")})
    return ca


@case
def test_hook_on_lowers_the_night_target():
    if not _HA_OK:
        return
    ca = _ca()
    ca._planner = _planner("on", 58.4)
    assert ca._resolve_target() == (58.4, "solar-plan")
    assert ca._resolve_target(plan=False) == (78.0, "fixed")   # what the planner plans against


@case
def test_hook_preview_and_stale_change_nothing():
    if not _HA_OK:
        return
    ca = _ca()
    ca._planner = _planner("preview", 58.4)
    assert ca._resolve_target() == (78.0, "fixed")
    ca._planner = _planner("on", 58.4, fresh=False)
    assert ca._resolve_target() == (78.0, "fixed")


@case
def test_hook_never_below_the_floor():
    if not _HA_OK:
        return
    ca = _ca()
    ca._planner = _planner("on", 20.0, floor=50.0)
    assert ca._resolve_target() == (50.0, "solar-plan")


@case
def test_hook_skips_balance_and_trip_nights():
    if not _HA_OK:
        return
    ca = _ca()
    ca._planner = _planner("on", 58.4)
    ca._balance_due = lambda: True
    ca._balance_target = lambda: 100.0
    ca._uncertain = lambda: False
    assert ca._resolve_target() == (100.0, "balance")


@case
def test_planner_inputs_use_the_unplanned_target():
    if not _HA_OK:
        return
    ca = _ca()
    ca._planner = _planner("on", 58.4)
    ca._opts[T.C.CA_BATTERY_KWH] = 82.56
    i = ca.planner_inputs()
    assert i["target_pct"] == 78.0 and i["soc"] == 55.0 and i["battery_kwh"] == 82.56
    assert ca.planner_input_entities() == ["sensor.soc"]


@case
def test_replans_when_the_soc_loads_after_a_restart():
    """No SOC on the first run: watch the SOC entity, re-plan once it has a
    value, and stop watching."""
    if not _HA_OK:
        return
    import asyncio
    import wallbox_gateway.solar_planner as SP

    class _Ev:
        def __init__(self, state):
            self.data = {"new_state": None if state is None else T.FakeState(state)}

    class _Hass:
        tasks = []
        def async_create_task(self, coro):
            self.tasks.append(coro); coro.close()

    class _CA:
        def planner_inputs(self):
            return {"soc": None, "target_pct": 78.0, "battery_kwh": 82.56}
        def planner_input_entities(self):
            return ["sensor.soc"]

    watched, unsubbed = {}, []
    def _track(hass, eids, cb):
        watched["eids"], watched["cb"] = eids, cb
        return lambda: unsubbed.append(True)

    orig = SP.async_track_state_change_event
    SP.async_track_state_change_event = _track
    try:
        pl = SP.SolarPlanner.__new__(SP.SolarPlanner)
        pl.hass, pl._ca, pl._input_unsub = _Hass(), _CA(), None
        pl._opts = lambda: {"solar_plan": "on"}
        asyncio.run(pl.async_compute())
        assert watched["eids"] == ["sensor.soc"] and pl._input_unsub is not None
        asyncio.run(pl.async_compute())          # a second miss doesn't stack a watch
        assert not unsubbed
        watched["cb"](_Ev("unavailable"))         # still loading: keep waiting
        assert not pl.hass.tasks and not unsubbed
        watched["cb"](_Ev("55"))                  # loaded: re-plan, stop watching
        assert len(pl.hass.tasks) == 1 and unsubbed == [True]
        assert pl._input_unsub is None
    finally:
        SP.async_track_state_change_event = orig


@case
def test_replans_when_the_outlook_updates():
    """17 Nov: after a restart the plan ran 13 s before Solar Comfort rebuilt
    its outlook; a rebuilt outlook now triggers a re-plan."""
    if not _HA_OK:
        return
    import asyncio
    import wallbox_gateway.solar_planner as SP

    class _Store:
        async def async_load(self):
            return {"home": {}, "fc": {}, "pattern_v": SP._PATTERN_V}

    class _Hass:
        tasks = []
        def async_create_task(self, coro):
            self.tasks.append(coro); coro.close()

    watched = {}
    def _track(hass, eids, cb):
        watched.setdefault("eids", []).append(eids); watched["cb"] = cb
        return lambda: None

    origs = (SP.async_track_state_change_event, SP.async_track_time_change,
             SP.async_track_time_interval)
    SP.async_track_state_change_event = _track
    SP.async_track_time_change = lambda *a, **k: (lambda: None)
    SP.async_track_time_interval = lambda *a, **k: (lambda: None)
    try:
        for src, expect in (("auto", True), ("forecast", False)):
            watched.clear()
            pl = SP.SolarPlanner.__new__(SP.SolarPlanner)
            pl.hass, pl._store, pl._unsubs, pl._home = _Hass(), _Store(), [], {}
            pl._ca = type("CA", (), {"_connected_entity": None})()   # no recorder seed
            opts = {"solar_plan": "on", "solar_source": src,
                    "solar_outlook_entity": "sensor.outlook"}
            pl._opts = lambda: opts
            pl.hass.tasks = []
            asyncio.run(pl.async_start())
            assert (["sensor.outlook"] in watched.get("eids", [])) is expect, src
        pl.hass.tasks = []
        pl._on_outlook_change(type("E", (), {"data": {"new_state": T.FakeState("unavailable")}})())
        assert not pl.hass.tasks
        pl._on_outlook_change(type("E", (), {"data": {"new_state": T.FakeState("14.4")}})())
        assert len(pl.hass.tasks) == 1
    finally:
        (SP.async_track_state_change_event, SP.async_track_time_change,
         SP.async_track_time_interval) = origs


@case
def test_home_schedule_replaces_the_learned_pattern():
    """A set schedule counts only its hours, with no history; unreadable text
    falls back to the learned pattern and says why."""
    if not _HA_OK:
        return
    import wallbox_gateway.solar_planner as SP
    pl = SP.SolarPlanner.__new__(SP.SolarPlanner)
    pl._home = {}                              # no history at all
    opts = {"solar_home_schedule": "Mon-Fri 17:00-08:00; Sat,Sun 00:00-24:00"}
    pl._opts = lambda: opts
    sched, err = pl._schedule()
    assert sched and err is None
    chance = pl._home_chance(date(2026, 11, 17), 14, sched)
    assert chance(_t(date(2026, 11, 18), 11)) == 0.0     # Wed 11:00, away
    assert chance(_t(date(2026, 11, 21), 11)) == 1.0     # Sat 11:00, home
    opts["solar_home_schedule"] = "Mox 10-12"
    sched, err = pl._schedule()
    assert sched is None and "Mox" in err
    assert pl._home_chance(date(2026, 11, 17), 14, sched)(_t(date(2026, 11, 21), 11)) is None
    opts["solar_home_schedule"] = "  "
    assert pl._schedule() == (None, None)


@case
def test_outlook_hours_capped_at_the_inverter():
    """17 Nov: an outlook said 5.72 kWh in one hour on a 5 kW inverter."""
    d = date(2026, 11, 18)
    out = {_t(d, 7): 2.9, _t(d, 8): 5.72, _t(d, 11): 6.81}
    assert SO.cap(out, 4.9) == {_t(d, 7): 2.9, _t(d, 8): 4.9, _t(d, 11): 4.9}
    assert SO.cap(out, None) == out and SO.cap(out, 0) == out


@case
def test_inverter_rating_replaces_the_learned_cap():
    """17 Nov: a 10 kW inverter learned only 7.7 kWh/h (export-limit throttling)."""
    if not _HA_OK:
        return
    import wallbox_gateway.solar_planner as SP
    pl = SP.SolarPlanner.__new__(SP.SolarPlanner)
    pl._max_hour_kwh = 7.7
    opts = {}
    pl._opts = lambda: opts
    assert pl._hour_cap() == 7.7
    opts["solar_inverter_kw"] = 10
    assert pl._hour_cap() == 10.0
    opts["solar_inverter_kw"] = ""
    assert pl._hour_cap() == 7.7


@case
def test_departure_hour_needs_both_half_hours():
    """Leaving about 9 (8:09 to 9:22 on weekdays): the 8:00 hour must not count
    the full hour when 8:30 is often away."""
    if not _HA_OK:
        return
    import wallbox_gateway.solar_planner as SP
    pl = SP.SolarPlanner.__new__(SP.SolarPlanner)
    pl._home = {}
    d = date(2026, 11, 18)
    probs = {_t(d, 8): 1.0, _t(d, 8, 30): 0.55, _t(d, 7): 1.0, _t(d, 7, 30): 1.0}
    orig = SP.HP.probability
    SP.HP.probability = lambda days, t, today, n: probs.get(t)
    try:
        chance = pl._home_chance(d, 14)
        assert chance(_t(d, 8)) == 0.55 and chance(_t(d, 7)) == 1.0
    finally:
        SP.HP.probability = orig


@case
def test_tiny_solar_reads_under_1_kwh():
    """18 Nov: 0.1 kWh of morning solar read "about 0 kWh"."""
    p = OP.Plan(need_kwh=23, floor_kwh=0, solar_kwh=0.1, tonight_kwh=23, target_pct=82)
    r = OP.explain(p, 57, 82, 50)
    assert "under 1 kWh" in r and "about 0" not in r, r
    p.solar_kwh = 4.4
    assert "about 4 kWh" in OP.explain(p, 57, 82, 50)


@case
def test_solar_left_today_from_now():
    d = date(2026, 11, 22)
    spare = {_t(d, 13): 4.0, _t(d, 14): 8.0, _t(d, 15): 1.0,
             _t(d, 12): 5.0, _t(date(2026, 11, 23), 10): 6.0}
    now = _t(d, 13, 30)
    # 13:00 half left (2.0) + 14:00 capped at 7.0 + 15:00 1.0; not 12:00, not tomorrow
    assert abs(SO.left_today(spare, now, 7.0) - 10.0) < 1e-9


@case
def test_location_pattern_recorded_and_used_for_car_use():
    """The car location entity (e.g. a BYD device_tracker) teaches when the car
    is out, also while home but unplugged; without one, plug-in history is used."""
    if not _HA_OK:
        return
    from homeassistant.util import dt as dt_util
    import wallbox_gateway.solar_planner as SP

    class _St:
        def __init__(self, state, changed):
            self.state, self.last_changed, self.attributes = state, changed, {}

    class _Hass:
        def __init__(self, m):
            self.states = type("S", (), {"get": lambda _s, e: m.get(e)})()
        def async_create_task(self, coro):
            coro.close()

    now = dt_util.now().replace(minute=30, second=20, microsecond=0)
    long_ago = now - timedelta(hours=5)
    states = {"device_tracker.car": _St("not_home", long_ago),
              "binary_sensor.plugged": _St("off", long_ago)}
    pl = SP.SolarPlanner.__new__(SP.SolarPlanner)
    pl.hass, pl._home, pl._loc, pl._fc = _Hass(states), {}, {}, {}
    pl._ca = type("CA", (), {"_connected_entity": "binary_sensor.plugged"})()
    opts = {"car_location_entity": "device_tracker.car"}
    pl._opts = lambda: opts
    pl._learn_production = lambda local: None
    pl._on_half_hour(now)
    slot = dt_util.as_local(now).replace(second=0) - timedelta(minutes=30)
    assert pl._loc[slot.date().isoformat()][HP.slot_of(slot)] == 0      # out
    states["device_tracker.car"] = _St("home", long_ago)
    pl._on_half_hour(now + timedelta(minutes=30))
    slot2 = slot + timedelta(minutes=30)
    assert pl._loc[slot2.date().isoformat()][HP.slot_of(slot2)] == 1     # home
    # usage_chance reads the location pattern when set, the plug-in one otherwise.
    seen = []
    orig = SP.HP.probability
    SP.HP.probability = lambda days, t, today, n: seen.append(days) or 0.5
    try:
        pl.usage_chance(now)
        opts.pop("car_location_entity")
        pl.usage_chance(now)
    finally:
        SP.HP.probability = orig
    assert seen[0] is pl._loc and seen[1] is pl._home


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
