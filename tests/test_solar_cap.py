"""Unit tests for the solar cap (Smart charge, hand back to solar). HA-free.
Run:  py tests/test_solar_cap.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import solar_cap as S  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn


def d(**kw):
    base = dict(cap=90, soc=85.0, charging=True, we_started=False, holding=False, plugged=True)
    base.update(kw)
    return S.decide(**base)


@case
def test_off_values():
    for v in (None, "", 0, 100, 120, "junk"):
        assert S.cap_of(v) is None, v
    assert S.cap_of("90") == 90.0


@case
def test_pauses_a_solar_charge_at_the_cap():
    assert d(soc=89.9) == S.NONE
    assert d(soc=90) == S.PAUSE
    assert d(soc=95) == S.PAUSE


@case
def test_never_touches_its_own_charge_or_an_idle_charger():
    assert d(soc=95, we_started=True) == S.NONE   # night top-up, trip, balance
    assert d(soc=95, charging=False) == S.NONE
    assert d(soc=95, cap=None) == S.NONE
    assert d(soc=None) == S.NONE


@case
def test_holds_until_unplug_drop_or_cap_off():
    assert d(holding=True, soc=92, charging=False) == S.NONE
    assert d(holding=True, soc=87.5, charging=False) == S.NONE   # within the hysteresis
    assert d(holding=True, soc=86.9, charging=False) == S.RELEASE
    assert d(holding=True, soc=92, plugged=False) == S.RELEASE
    assert d(holding=True, soc=92, cap=None) == S.RELEASE
    assert d(holding=True, soc=None) == S.NONE                   # unknown SOC: keep holding


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
