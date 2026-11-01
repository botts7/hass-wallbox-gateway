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


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
