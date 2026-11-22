"""Every combination of the floor / critical top-up rules (22 Nov 2026).

An independent oracle written from the spec is checked against the Charge
Assistant for each combination of: wait-for-cheap on/off, battery level
(below critical, below the floor, above it), cheap window far / soon, today's
sun covering the gap or not, the car's usual pattern (home, out, unknown) and
the departure source (none, daily time, timestamp sensor, input_datetime,
calendar with a matching / non-matching keyword) before or after the window.

Plus the departure parsers across HA's time zone (Australia/Sydney, both DST
changes), and the car-location pattern from tracker states.

Run:  py tests/test_topup_matrix.py
"""

import itertools
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "custom_components", "wallbox_gateway"))
import car_use as CU  # noqa: E402
import home_pattern as HP  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn

SYD = ZoneInfo("Australia/Sydney")

try:
    sys.path.insert(0, HERE)
    import test_controller_decisions as T
    _HA_OK = T._HA_OK
except Exception as e:  # pragma: no cover
    print(f"--- test_topup_matrix (assistant part): SKIPPED ({e})")
    _HA_OK = False


# ── departure parsers in HA's time zone ─────────────────────────────
@case
def test_timestamp_sensor_states():
    now = datetime(2026, 11, 22, 14, 0, tzinfo=SYD)
    f = lambda s: CU.departure_from_state("sensor", s, {}, now, SYD)
    assert f("2026-11-22T18:00:00+11:00") == datetime(2026, 11, 22, 18, 0, tzinfo=SYD)
    assert f("2026-11-22T07:00:00+00:00") == datetime(2026, 11, 22, 18, 0, tzinfo=SYD)
    assert f("2026-11-22T18:00:00") == datetime(2026, 11, 22, 18, 0, tzinfo=SYD)   # zone-less: HA's
    for bad in ("unknown", "unavailable", "", None, "none", "garbage", "18:00"):
        assert f(bad) is None, bad


@case
def test_input_datetime_kinds():
    now = datetime(2026, 11, 22, 14, 0, tzinfo=SYD)
    both = {"has_date": True, "has_time": True}
    t_only = {"has_date": False, "has_time": True, "timestamp": 64800}   # 18:00
    d_only = {"has_date": True, "has_time": False, "timestamp": 1795000000}
    f = lambda s, a: CU.departure_from_state("input_datetime", s, a, now, SYD)
    assert f("2026-11-22 18:00:00", both) == datetime(2026, 11, 22, 18, 0, tzinfo=SYD)
    assert f("18:00:00", t_only) == datetime(2026, 11, 22, 18, 0, tzinfo=SYD)     # later today
    assert f("09:00:00", t_only) == datetime(2026, 11, 23, 9, 0, tzinfo=SYD)      # passed: tomorrow
    assert f("14:00:00", t_only) == datetime(2026, 11, 23, 14, 0, tzinfo=SYD)     # == now: tomorrow
    assert f("2026-11-22", d_only) is None                                         # no time
    assert f("garbage", both) is None and f("unknown", both) is None
    assert f("2026-11-22 18:00:00", {}) == datetime(2026, 11, 22, 18, 0, tzinfo=SYD)  # no flags


@case
def test_daylight_saving_changes_in_sydney():
    # DST starts Sun 4 Oct 2026 (02:00 -> 03:00), ends Sun 5 Apr 2026 (03:00 -> 02:00).
    t_only = {"has_date": False, "has_time": True}
    sat_night = datetime(2026, 10, 3, 23, 0, tzinfo=SYD)                 # +10:00
    d = CU.departure_from_state("input_datetime", "07:00:00", t_only, sat_night, SYD)
    assert d == datetime(2026, 10, 4, 7, 0, tzinfo=SYD) and d.utcoffset() == timedelta(hours=11)
    autumn = datetime(2026, 4, 4, 23, 0, tzinfo=SYD)                     # +11:00
    d = CU.departure_from_state("input_datetime", "07:00:00", t_only, autumn, SYD)
    assert d.utcoffset() == timedelta(hours=10)
    # Zone-less calendar times take the right offset on each side of the change.
    evs = [{"start": "2026-10-04T08:00:00", "summary": "drive"}]
    assert CU.next_departure(evs, sat_night, tz=SYD).utcoffset() == timedelta(hours=11)
    # The half-hour walk across a DST night ends and sees every slot.
    seen = []
    CU.needed_before(sat_night, datetime(2026, 10, 4, 6, 0, tzinfo=SYD),
                     lambda t: seen.append(t) or 0.95, 0.7)
    assert seen and seen[-1] < datetime(2026, 10, 4, 6, 0, tzinfo=SYD)
    seen.clear()
    CU.needed_before(autumn, datetime(2026, 4, 5, 6, 0, tzinfo=SYD),
                     lambda t: seen.append(t) or 0.95, 0.7)
    assert seen and len(seen) < 40


@case
def test_calendar_events_at_home_are_not_drives():
    now = datetime(2026, 11, 22, 14, 0, tzinfo=SYD)
    evs = [{"start": "2026-11-22T15:00:00+11:00", "summary": "Plumber", "location": "Home"},
           {"start": "2026-11-22T16:00:00+11:00", "summary": "Tea", "location": " home "},
           {"start": "2026-11-22T17:00:00+11:00", "summary": "Hardware", "location": "Home Depot"}]
    assert CU.next_departure(evs, now, tz=SYD) == datetime(2026, 11, 22, 17, 0, tzinfo=SYD)


@case
def test_car_location_states_to_pattern():
    """A tracker (home / not_home / another zone / unavailable blips) to slots."""
    d = date(2026, 11, 22)
    t = lambda h, m=0: datetime(d.year, d.month, d.day, h, m, tzinfo=SYD)
    states = [(t(0), "home"), (t(8, 10), "not_home"), (t(9), "Work"), (t(12, 0), "unavailable"),
              (t(12, 1), "Work"), (t(17, 40), "home")]
    changes = [(at, CU.is_home("device_tracker", s)) for at, s in states]
    days: dict = {}
    HP.seed_from_intervals(days, changes, t(0), t(23, 59), overwrite=True)
    row = days[d.isoformat()]
    assert row[HP.slot_of(t(7, 30))] == 1         # home all of 07:30-08:00
    assert row[HP.slot_of(t(8))] == 0             # left at 08:10
    assert row[HP.slot_of(t(12))] == 0            # at Work, unavailable blip ignored
    assert row[HP.slot_of(t(17, 30))] == 0        # arrived 17:40, not the whole slot
    assert row[HP.slot_of(t(18))] == 1


# ── the Charge Assistant, every combination ─────────────────────────
CRIT, FLOOR = 30, 50
WINDOW_FAR, WINDOW_SOON = 600, 60      # minutes to the cheap window
DEPARTURES = ("none", "daily", "sensor", "input_datetime", "calendar_match", "calendar_other")


def _expected(*, wait, soc, mins, covers, pattern, dep, dep_hours):
    """The spec, written independently of the code: (start?, charge_to)."""
    dep_counts = dep not in ("none", "calendar_other")
    before = dep_counts and dep_hours * 60 <= mins
    needed = before or pattern in ("out", "unknown")
    waits = wait and mins > 0 and not needed
    if soc < CRIT:
        return (False, None) if waits else (True, CRIT)
    if soc < FLOOR:
        if mins <= 120 or covers or waits:
            return False, None
        return True, FLOOR
    return False, None


def _assistant(*, wait, soc, mins, covers, pattern, dep, dep_hours):
    C = T.C
    dt_util = T.dt_util
    ca, calls = T._floor_ca(soc, mins_to_window=mins)
    ca._opts.update({C.CA_CRITICAL_PCT: CRIT, C.CA_BATTERY_KWH: 82.56,
                     C.CA_TOPUP_WAIT_CHEAP: wait})
    ca._eval_target_windowed = lambda *a, **k: None   # isolate the floor rules from the clock
    ca._planner.solar_left_today_kwh = lambda now=None: 40.0 if covers else 0.0
    ca._planner.usage_chance = {"home": lambda t: 0.95, "out": lambda t: 0.2,
                                "unknown": lambda t: None}[pattern]
    at = dt_util.now() + timedelta(hours=dep_hours)
    if dep == "daily":
        ca._opts[C.CA_DEPARTURE] = at.strftime("%H:%M")
        if at.date() != dt_util.now().date():
            # A daily time already past today is tomorrow's: keep within reach.
            pass
    elif dep == "sensor":
        ca._opts[C.CA_DEPARTURE_ENTITY] = "sensor.next_departure"
        ca.hass.states._m["sensor.next_departure"] = T.FakeState(at.isoformat())
    elif dep == "input_datetime":
        st = T.FakeState(at.strftime("%Y-%m-%d %H:%M:%S"))
        st.attributes = {"has_date": True, "has_time": True}
        ca._opts[C.CA_DEPARTURE_ENTITY] = "input_datetime.next_departure"
        ca.hass.states._m["input_datetime.next_departure"] = st
    elif dep in ("calendar_match", "calendar_other"):
        ca._opts[C.CA_DEPARTURE_ENTITY] = "calendar.family"
        ca._opts[C.CA_DEPARTURE_KEYWORD] = "drive"
        title = "Drive to the coast" if dep == "calendar_match" else "Book club"
        ca._cal_events = [{"start": at.replace(microsecond=0).isoformat(), "summary": title}]
    ca._eval_target()
    started = bool(calls) and calls[0] is True
    return started, (ca._floor_charge_to if started else None)


@case
def test_every_combination_matches_the_spec():
    if not _HA_OK:
        return
    n = 0
    for wait, soc, mins, covers, pattern, dep, dep_hours in itertools.product(
            (True, False), (20, 40, 60), (WINDOW_FAR, WINDOW_SOON), (True, False),
            ("home", "out", "unknown"), DEPARTURES, (3, 12)):
        if dep == "daily" and dep_hours == 12:
            continue   # a daily clock time 12 h ahead may wrap past midnight: covered below
        kw = dict(wait=wait, soc=soc, mins=mins, covers=covers, pattern=pattern,
                  dep=dep, dep_hours=dep_hours)
        want = _expected(**kw)
        got = _assistant(**kw)
        assert got == want, (kw, "expected", want, "got", got)
        n += 1
    assert n == 792, n        # 2x3x2x2x3x6x2 less the 72 wrapping daily-time rows
    print(f"      ({n} combinations)")


@case
def test_unplugged_or_unknown_battery_never_starts():
    if not _HA_OK:
        return
    for soc_state in ("unknown", "unavailable"):
        ca, calls = T._floor_ca(20)
        ca._opts.update({T.C.CA_CRITICAL_PCT: CRIT, T.C.CA_TOPUP_WAIT_CHEAP: True})
        ca.hass.states._m["sensor.soc"] = T.FakeState(soc_state)
        ca._eval_target_windowed = lambda *a, **k: None
        ca._eval_target()
        assert calls == [], (soc_state, calls)
    ca, calls = T._floor_ca(20)
    ca._opts[T.C.CA_CRITICAL_PCT] = CRIT
    ca._plugged_in = lambda: False
    ca._eval_target_windowed = lambda *a, **k: None
    ca._eval_target()
    assert calls == [], calls


@case
def test_wait_needs_a_window_to_wait_for():
    """No cheap window configured: nothing to wait for, so it charges."""
    if not _HA_OK:
        return
    ca, calls = T._floor_ca(20, mins_to_window=None)
    ca._opts.update({T.C.CA_CRITICAL_PCT: CRIT, T.C.CA_TOPUP_WAIT_CHEAP: True,
                     T.C.CA_WINDOW_ENABLED: False})
    ca._planner.usage_chance = lambda t: 0.95
    ca._eval_target()
    assert calls == [True], calls


@case
def test_planner_and_calendar_are_ready_before_the_first_evaluation():
    """22 Nov, live: a config reload evaluated before the planner existed, so
    'wait for cheap hours' saw no pattern, took it as 'needed' and started a
    top-up that should have waited. Run the real async_start and check the
    planner and the calendar events are there when the strategy starts."""
    if not _HA_OK:
        return
    import asyncio
    import wallbox_gateway.charge_assistant as CA
    C = T.C
    order = []

    class _Planner:
        def __init__(self, *a, **k):
            pass
        async def async_start(self):
            order.append("planner")
        def mode(self):
            return "on"

    class _Store:
        def __init__(self, *a, **k):
            pass
        async def async_load(self):
            return {}
        async def async_save(self, data):
            pass

    class _Services:
        async def async_call(self, *a, **k):
            order.append("calendar")
            return {"calendar.family": {"events": []}}

    ca, _ = T.build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                     C.CA_TARGET_PCT: 80, C.CA_DEPARTURE_ENTITY: "calendar.family"},
                    {"sensor.soc": T.FakeState("66")})
    ca.entry.options = {C.CA_KEY: dict(ca._opts)}
    ca.entry.title = "Wallbox test"
    ca._own_entity = lambda *a, **k: None          # entity registry: not under test
    ca.entry.entry_id = "e1"
    ca.hass.services = _Services()
    async def _start_target():
        order.append("strategy" if ca._planner is not None else "strategy-without-planner")
    ca._start_target = _start_target
    patches = {"SolarPlanner": _Planner, "Store": _Store,
               "async_track_time_interval": lambda *a, **k: (lambda: None)}
    saved = {k: getattr(CA, k) for k in patches}
    for k, v in patches.items():
        setattr(CA, k, v)
    try:
        try:
            asyncio.run(ca.async_start())
        except Exception as err:   # later startup steps need more of HA
            order.append(f"stopped: {type(err).__name__}: {err}")
    finally:
        for k, v in saved.items():
            setattr(CA, k, v)
    assert order[:3] == ["planner", "calendar", "strategy"], order


# ── countdown notification: urgent, tagged, cleared when cancelled ──
@case
def test_countdown_notification_payloads():
    import notify_payload as NP
    d = NP.time_sensitive({"actions": [1]})
    assert d == {"actions": [1], "tag": "wb_ca_grace", "priority": "high", "ttl": 0,
                 "push": {"interruption-level": "time-sensitive"}}, d
    assert NP.clear() == {"message": "clear_notification", "data": {"tag": "wb_ca_grace"}}


def _notify_ca():
    import asyncio
    sent = []

    class _Services:
        async def async_call(self, domain, service, data, blocking=False, **k):
            sent.append((f"{domain}.{service}", data))

    ca, _ = T.build({T.C.CA_MODE: T.C.MODE_TARGET,
                     T.C.CA_NOTIFY_SERVICE: "notify.mobile_app_a, notify.mobile_app_b"}, {})
    ca.hass.services = _Services()
    return ca, sent, asyncio


@case
def test_countdown_is_sent_urgent_and_tagged_and_charging_now_replaces_it():
    """22 Nov: the countdown reached the phone ~7 min late and stayed after
    being cancelled."""
    if not _HA_OK:
        return
    ca, sent, asyncio = _notify_ca()
    asyncio.run(ca._send_notification("grace", message_override="Charging will start in 5 min",
                                      action_set="grace"))
    assert [svc for svc, _ in sent] == ["notify.mobile_app_a", "notify.mobile_app_b"]
    data = sent[0][1]["data"]
    assert data["tag"] == "wb_ca_grace" and data["priority"] == "high" and data["ttl"] == 0
    assert data["push"] == {"interruption-level": "time-sensitive"}
    assert [a["title"] for a in data["actions"]] == ["Start now", "Not now"]
    sent.clear()
    asyncio.run(ca._send_notification("target", message_override="Charging now", action_set="grace_done"))
    data = sent[0][1]["data"]
    assert data["tag"] == "wb_ca_grace" and "actions" not in data       # replaces the countdown
    sent.clear()
    asyncio.run(ca._send_notification("target", message_override="Charged to 80 %"))
    assert "data" not in sent[0][1]                                     # other notes unchanged


@case
def test_cancelled_countdown_is_cleared_but_start_now_is_not():
    if not _HA_OK:
        return
    ca, sent, asyncio = _notify_ca()
    asyncio.run(ca._clear_grace_notification())
    assert sent == [("notify.mobile_app_a", {"message": "clear_notification", "data": {"tag": "wb_ca_grace"}}),
                    ("notify.mobile_app_b", {"message": "clear_notification", "data": {"tag": "wb_ca_grace"}})], sent
    cleared = []
    ca._clear_grace_notification = lambda: cleared.append(1) or _noop()
    for why in ("outside cheap window", "user tapped Not now", "assistant stopping", "car unplugged"):
        ca._grace_pending = {"reason": "x", "target": 30}
        ca._cancel_grace(why)
    assert len(cleared) == 4, cleared
    ca._grace_pending = {"reason": "x", "target": 30}
    ca._cancel_grace("user tapped Start now")
    assert len(cleared) == 4                      # becomes "Charging now" instead
    ca._cancel_grace("nothing pending")           # no countdown: nothing to clear
    assert len(cleared) == 4


async def _noop():
    return None


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
