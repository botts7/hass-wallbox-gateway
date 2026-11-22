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


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
