"""Unit tests for skipping the overnight grid charge on chosen nights.

A car at or above the chosen level isn't charged from the grid on the chosen
nights, so the weekend is left to solar. HA-free.
Run:  py tests/test_night_skip.py
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import night_skip as N  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn


FRI_EVE = datetime(2026, 11, 13, 21, 0)   # Friday 21:00
SAT_0030 = datetime(2026, 11, 14, 0, 30)  # still Friday night
SAT_EVE = datetime(2026, 11, 14, 21, 0)
SUN_EVE = datetime(2026, 11, 15, 21, 0)
MON_0300 = datetime(2026, 11, 16, 3, 0)   # Sunday night


@case
def test_night_runs_noon_to_noon():
    assert N.night_of(FRI_EVE) == "fri"
    assert N.night_of(SAT_0030) == "fri"
    assert N.night_of(datetime(2026, 11, 14, 11, 59)) == "fri"
    assert N.night_of(datetime(2026, 11, 14, 12, 0)) == "sat"
    assert N.night_of(MON_0300) == "sun"


@case
def test_skips_on_chosen_nights_at_or_above_the_level():
    weekend = ["fri", "sat"]
    assert N.skip_tonight(65, 60, weekend, FRI_EVE)
    assert N.skip_tonight(60, 60, weekend, SAT_0030)      # at the level counts
    assert N.skip_tonight(70, 60, weekend, SAT_EVE)
    assert not N.skip_tonight(59.5, 60, weekend, SAT_EVE)  # below: charge as usual
    assert not N.skip_tonight(90, 60, weekend, SUN_EVE)    # not a chosen night
    assert not N.skip_tonight(90, 60, weekend, MON_0300)


@case
def test_off_or_unknown_charges_as_usual():
    assert not N.skip_tonight(90, None, ["fri"], FRI_EVE)
    assert not N.skip_tonight(90, 0, ["fri"], FRI_EVE)
    assert not N.skip_tonight(90, "", ["fri"], FRI_EVE)
    assert not N.skip_tonight(90, "junk", ["fri"], FRI_EVE)
    assert not N.skip_tonight(None, 60, ["fri"], FRI_EVE)  # SOC unavailable


@case
def test_no_nights_chosen_means_every_night():
    for t in (FRI_EVE, SUN_EVE, MON_0300):
        assert N.skip_tonight(80, 60, [], t)
        assert N.skip_tonight(80, 60, None, t)
    assert N.nights_of(["sat", "bogus", "fri"]) == ("fri", "sat")


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
