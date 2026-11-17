"""Learned charger power (charge_rate.py).

Run:  py tests/test_charge_rate.py
"""

import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "custom_components", "wallbox_gateway"))
import charge_rate as CR  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn


def _fill(samples, start, minutes, kw):
    for m in range(minutes):
        CR.add_sample(samples, start + m * 60, kw)


@case
def test_learns_full_speed_and_ignores_slow_charging():
    """17 Nov: full speed reads 7.03 to 7.05 kW at the wall; solar mornings
    run 1.5 to 3 kW and must not drag it down."""
    s = []
    assert CR.learned_kw(s) is None
    _fill(s, 0, 20, 7.04)
    assert CR.learned_kw(s) is None                       # not enough yet
    _fill(s, 3600, 40, 7.05)
    _fill(s, 7200, 90, 2.2)                               # solar-throttled
    assert CR.learned_kw(s) == 7.05


@case
def test_one_sample_a_minute_while_charging_only():
    s = []
    assert CR.add_sample(s, 0, 7.0)
    assert not CR.add_sample(s, 30, 7.0)                  # too soon
    assert not CR.add_sample(s, 120, 0.4)                 # not charging
    assert not CR.add_sample(s, 180, None)
    assert CR.add_sample(s, 240, 7.0) and len(s) == 2


@case
def test_old_samples_drop_off():
    s = []
    _fill(s, 0, 40, 11.0)                                 # an old, faster charger
    _fill(s, CR.KEEP_SECS + 3600, 40, 7.0)
    assert CR.learned_kw(s) == 7.0


@case
def test_three_phase_rate():
    s = []
    _fill(s, 0, 60, 21.8)
    assert CR.learned_kw(s) == 21.8


@case
def test_battery_side_power():
    assert abs(CR.battery_kw(7.05, 0.88) - 6.204) < 1e-9
    assert CR.battery_kw(7.0, 3) == 7.0 * 0.9             # bad efficiency falls back


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
