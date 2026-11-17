"""Manual home schedule for the solar planner (home_schedule.py).

Run:  py tests/test_home_schedule.py
"""

import os
import sys
from datetime import datetime

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "custom_components", "wallbox_gateway"))
import home_schedule as HS  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn

# 16 Nov 2026 is a Monday.
def _at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, 16 + day, hh, mm)


@case
def test_empty_is_no_schedule():
    assert HS.parse("") == [] and HS.parse(None) == [] and HS.parse(" ; \n") == []


@case
def test_weekday_evenings_overnight_and_weekends():
    s = HS.parse("Mon-Fri 17:00-08:00; Sat,Sun 00:00-24:00")
    assert not HS.is_home(s, _at(2, 12))     # Wed noon: at work
    assert HS.is_home(s, _at(2, 18))         # Wed evening
    assert HS.is_home(s, _at(3, 7, 30))      # Thu morning, from Wed night
    assert not HS.is_home(s, _at(3, 8))      # left at 08:00
    assert HS.is_home(s, _at(5, 12))         # Sat noon


@case
def test_monday_morning_after_a_weekend():
    s = HS.parse("Mon-Fri 17:00-08:00; Sat,Sun 00:00-24:00")
    # Fri 17:00-08:00 spills into Sat; nothing spills into Monday morning.
    assert not HS.is_home(s, _at(0, 7))


@case
def test_several_ranges_daily_and_wrapping_day_range():
    s = HS.parse("Wed 09:00-12:00, 13:00-15:30")
    assert HS.is_home(s, _at(2, 10)) and not HS.is_home(s, _at(2, 12, 30))
    assert HS.is_home(s, _at(2, 15)) and not HS.is_home(s, _at(2, 15, 30))
    s = HS.parse("daily 10-14")
    assert all(HS.is_home(s, _at(d, 11)) for d in range(7))
    s = HS.parse("Fri-Mon 09:00-17:00")
    assert {d for d in range(7) if HS.is_home(s, _at(d, 10))} == {4, 5, 6, 0}


@case
def test_hour_chance_in_halves():
    s = HS.parse("Tue 10:30-12:00")
    assert HS.hour_chance(s, _at(1, 10)) == 0.5
    assert HS.hour_chance(s, _at(1, 11)) == 1.0
    assert HS.hour_chance(s, _at(1, 12)) == 0.0


@case
def test_bad_text_says_what_is_wrong():
    for bad, word in (("Mox 10:00-12:00", "Mox"), ("Mon", "no times"),
                      ("Mon 25:00-26:00", "bad time"), ("Mon 10:75-11:00", "bad time")):
        try:
            HS.parse(bad)
        except HS.ScheduleError as e:
            assert word in str(e), (bad, e)
        else:
            raise AssertionError(f"accepted {bad!r}")


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
