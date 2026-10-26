"""Unit tests for the charger current ceiling (#39 follow-up).

US Pulsar Plus units report 40 A or 48 A; a fixed 32 A cap held them back.
Every current setter now uses current_limits. HA-free.
Run:  py tests/test_current_limits.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import current_limits as C  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn


@case
def test_us_40a_from_realtime_or_status():
    assert C.current_ceiling({"charger_realtime": {"max_available_current": 40}}) == 40
    assert C.current_ceiling({"charger_status": {"max_available_current": 48}}) == 48
    # realtime wins when both are present
    assert C.current_ceiling({"charger_realtime": {"max_available_current": 40},
                              "charger_status": {"max_available_current": 32}}) == 40


@case
def test_installer_limit_below_32():
    assert C.current_ceiling({"charger_realtime": {"max_available_current": 16}}) == 16


@case
def test_unknown_or_bad_falls_back_to_32():
    assert C.current_ceiling(None) == 32
    assert C.current_ceiling({}) == 32
    assert C.current_ceiling({"charger_realtime": {"max_available_current": 0}}) == 32
    assert C.current_ceiling({"charger_realtime": {"max_available_current": 500}}) == 32
    assert C.current_ceiling({"charger_realtime": {"max_available_current": True}}) == 32
    assert C.current_ceiling({"charger_realtime": {"max_available_current": "40"}}) == 32


@case
def test_camelcase_spelling():
    assert C.ceiling_from({"maxAvailableCurrent": 40}) == 40


@case
def test_clamp_current():
    assert C.clamp_current(40, 40) == 40
    assert C.clamp_current(45, 40) == 40
    assert C.clamp_current(3, 40) == 6
    assert C.clamp_current(40, 32) == 32


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
