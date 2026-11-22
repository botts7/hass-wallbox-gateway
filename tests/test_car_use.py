"""Will the car be needed before the cheap window? (car_use.py)

Run:  py tests/test_car_use.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "custom_components", "wallbox_gateway"))
import car_use as CU  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn

TZ = timezone(timedelta(hours=11))
def _t(hh, mm=0, day=22):
    return datetime(2026, 11, day, hh, mm, tzinfo=TZ)


@case
def test_home_states_by_domain():
    assert CU.is_home("device_tracker", "home") is True
    assert CU.is_home("device_tracker", "not_home") is False
    assert CU.is_home("device_tracker", "Work") is False        # another zone
    assert CU.is_home("person", "home") is True
    assert CU.is_home("binary_sensor", "on") is True
    assert CU.is_home("binary_sensor", "off") is False
    assert CU.is_home("device_tracker", "unavailable") is None
    assert CU.is_home("binary_sensor", None) is None


@case
def test_home_all_evening_waits_for_midnight():
    """Sunday 16:30 at 28 %: usually home all evening -> not needed, wait."""
    assert CU.needed_before(_t(16, 30), _t(0, 0, 23), lambda t: 0.95, 0.7) is False


@case
def test_usual_evening_drive_means_needed():
    """Usually out around 18:00 -> needed before midnight, charge now."""
    chance = lambda t: 0.2 if t.hour == 18 else 0.95
    assert CU.needed_before(_t(16, 30), _t(0, 0, 23), chance, 0.7) is True


@case
def test_departure_before_the_window_means_needed():
    home = lambda t: 0.95
    assert CU.needed_before(_t(16, 30), _t(0, 0, 23), home, 0.7, [_t(20, 0)]) is True
    # A departure after the window starts doesn't count.
    assert CU.needed_before(_t(16, 30), _t(0, 0, 23), home, 0.7, [_t(7, 0, 23)]) is False
    assert CU.needed_before(_t(16, 30), _t(0, 0, 23), home, 0.7, [None]) is False


@case
def test_no_pattern_yet_means_needed():
    assert CU.needed_before(_t(16, 30), _t(0, 0, 23), lambda t: None, 0.7) is True


@case
def test_already_in_or_past_the_window():
    assert CU.needed_before(_t(0, 30, 23), _t(0, 0, 23), lambda t: 0.0, 0.7) is False


# ── calendar departures (formats from HA's calendar.get_events) ─────
NOW = _t(14, 0)


@case
def test_calendar_next_timed_event_is_the_departure():
    evs = [{"start": "2026-11-22T18:00:00+11:00", "end": "2026-11-22T19:00:00+11:00",
            "summary": "Dinner at Mum's"},
           {"start": "2026-11-22T16:30:00+11:00", "summary": "Dentist"}]
    assert CU.next_departure(evs, NOW) == _t(16, 30)     # earliest, not list order


@case
def test_calendar_skips_all_day_past_and_malformed():
    evs = [{"start": "2026-11-22", "end": "2026-11-23", "summary": "Birthday"},  # all-day
           {"start": "2026-11-22T09:00:00+11:00", "summary": "Gym"},            # past
           {"start": "2026-11-22T14:00:00+11:00", "summary": "Now"},            # == now
           {"start": "not a date", "summary": "Bad"},
           {"summary": "No start"}, "junk", None,
           {"start": "2026-11-23T07:30:00+11:00", "summary": "Work"}]
    assert CU.next_departure(evs, NOW) == _t(7, 30, 23)
    assert CU.next_departure([], NOW) is None and CU.next_departure(None, NOW) is None


@case
def test_calendar_keyword_matches_summary_description_location_any_case():
    evs = [{"start": "2026-11-22T15:00:00+11:00", "summary": "Call with bank"},
           {"start": "2026-11-22T17:00:00+11:00", "summary": "Flight", "location": "Airport T2"},
           {"start": "2026-11-22T19:00:00+11:00", "summary": "x", "description": "DRIVE to Bob's"}]
    assert CU.next_departure(evs, NOW, "airport") == _t(17, 0)
    assert CU.next_departure(evs, NOW, "drive") == _t(19, 0)
    assert CU.next_departure(evs, NOW, " Drive ") == _t(19, 0)
    assert CU.next_departure(evs, NOW, "") == _t(15, 0)          # empty = any event
    assert CU.next_departure(evs, NOW, "holiday") is None


@case
def test_calendar_naive_and_utc_times():
    evs = [{"start": "2026-11-22T17:00:00", "summary": "naive local"}]
    assert CU.next_departure(evs, NOW) == _t(17, 0)
    evs = [{"start": "2026-11-22T06:00:00+00:00", "summary": "utc"}]    # = 17:00 local
    assert CU.next_departure(evs, NOW) == _t(17, 0)


@case
def test_calendar_entity_attributes_fallback():
    attrs = {"message": "Airport run", "all_day": False, "start_time": "2026-11-22 18:00:00",
             "end_time": "2026-11-22 19:00:00", "location": "", "description": ""}
    ev = CU.event_from_attributes(attrs, TZ)
    assert CU.next_departure([ev], NOW, "airport") == _t(18, 0)
    assert CU.event_from_attributes({**attrs, "all_day": True}, TZ) is None
    assert CU.event_from_attributes({"message": "x"}, TZ) is None
    assert CU.event_from_attributes({**attrs, "start_time": "garbage"}, TZ) is None
    assert CU.event_from_attributes(None, TZ) is None


@case
def test_calendar_departure_feeds_needed_before():
    home = lambda t: 0.95
    dep = CU.next_departure([{"start": "2026-11-22T18:00:00+11:00", "summary": "drive"}], NOW)
    assert CU.needed_before(NOW, _t(0, 0, 23), home, 0.7, [dep]) is True
    dep = CU.next_departure([{"start": "2026-11-23T08:00:00+11:00", "summary": "drive"}], NOW)
    assert CU.needed_before(NOW, _t(0, 0, 23), home, 0.7, [dep]) is False


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
