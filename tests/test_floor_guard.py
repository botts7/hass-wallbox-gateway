"""Unit tests for keeping the car at or above the floor at any time. HA-free.
Run:  py tests/test_floor_guard.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import floor_guard as F  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn


def d(**kw):
    base = dict(floor=50, soc=40.0, charging=False, plugged=True, floor_charging=False, mins_to_window=600)
    base.update(kw)
    return F.decide(**base)


@case
def test_starts_below_the_floor_outside_the_window():
    assert d() == F.START
    assert d(mins_to_window=None) == F.START          # no cheap window at all
    assert d(mins_to_window=121) == F.START


@case
def test_waits_for_a_window_that_is_on_or_close():
    assert d(mins_to_window=0) == F.NONE              # in the window: normal plan
    assert d(mins_to_window=90) == F.NONE             # starts within 2 h


@case
def test_leaves_it_alone_when_not_needed():
    assert d(soc=50) == F.NONE
    assert d(charging=True) == F.NONE                 # already charging (solar or ours)
    assert d(plugged=False) == F.NONE
    assert d(plugged=None) == F.NONE
    assert d(soc=None) == F.NONE
    assert d(floor=None) == F.NONE


@case
def test_runs_to_the_floor_then_stops():
    assert d(floor_charging=True, charging=True, soc=45) == F.HOLD
    assert d(floor_charging=True, charging=True, soc=50) == F.STOP
    assert d(floor_charging=True, plugged=False) == F.STOP
    assert d(floor_charging=True, floor=None) == F.STOP
    assert d(floor_charging=True, charging=False, soc=45) == F.NONE   # starting up (grace)


@case
def test_waits_for_solar_that_will_get_there():
    """22 Nov follow-up: below the floor by day, but today's sun will lift it."""
    kw = dict(floor=50, soc=47, charging=False, plugged=True, floor_charging=False,
              mins_to_window=600)
    assert F.decide(**kw, solar_covers=True) == F.NONE
    assert F.decide(**kw, solar_covers=False) == F.START


@case
def test_critical_level_charges_whatever_the_sun_or_window():
    kw = dict(floor=50, charging=False, plugged=True, floor_charging=False)
    # Below critical: charge now, even if the sun would cover it or the window is close.
    assert F.decide(**kw, soc=15, mins_to_window=60, solar_covers=True, critical=20) == F.CRITICAL
    # Above critical but below the floor: the normal floor rules.
    assert F.decide(**kw, soc=30, mins_to_window=600, solar_covers=True, critical=20) == F.NONE
    assert F.decide(**kw, soc=30, mins_to_window=600, solar_covers=False, critical=20) == F.START
    # A critical level at or above the floor is ignored.
    assert F.decide(**kw, soc=30, mins_to_window=600, solar_covers=True, critical=60) == F.NONE
    # Works without a floor (planner off).
    assert F.decide(floor=None, charging=False, plugged=True, floor_charging=False,
                    soc=15, mins_to_window=600, critical=20) == F.CRITICAL


@case
def test_critical_charge_stops_at_the_critical_level():
    kw = dict(floor=50, plugged=True, floor_charging=True, mins_to_window=600, critical=20)
    assert F.decide(**kw, soc=18, charging=True, charge_to=20) == F.HOLD
    assert F.decide(**kw, soc=20, charging=True, charge_to=20) == F.STOP
    assert F.decide(**kw, soc=40, charging=True, charge_to=50) == F.HOLD   # a floor charge


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
