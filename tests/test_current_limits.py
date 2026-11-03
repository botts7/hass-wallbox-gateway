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


@case
def test_saved_gateway_ceiling_before_first_poll():
    # Forum, 48 A Pulsar Plus: right after a gateway reboot neither realtime nor
    # status carries the ceiling yet; the gateway's saved one is used.
    d = {"charger_realtime": None, "charger_status": {"cur": 48},
         "raw_status": {"max_current_ceiling": 48}}
    assert C.current_ceiling(d) == 48
    d["raw_status"]["max_current_ceiling"] = -1          # not known yet
    assert C.current_ceiling(d) == 32
    d["charger_realtime"] = {"max_available_current": 40}  # live value wins
    d["raw_status"]["max_current_ceiling"] = 48
    assert C.current_ceiling(d) == 40


@case
def test_user_max_keeps_a_saved_value():
    assert C.user_max(None, 48) == 48   # unset = the full ceiling
    assert C.user_max("", 48) == 48
    assert C.user_max(32, 48) == 32     # kept: it may be deliberate
    assert C.user_max(24, 48) == 24
    assert C.user_max(60, 48) == 48     # never above the ceiling
    assert C.user_max(3, 48) == 6


@case
def test_below_ceiling_flags_only_a_lower_saved_value():
    assert C.below_ceiling(32, 48) == 32     # tell the user
    assert C.below_ceiling(48, 48) is None
    assert C.below_ceiling(32, 32) is None   # a 32 A charger: nothing to say
    assert C.below_ceiling(None, 48) is None
    assert C.below_ceiling("", 48) is None
    assert C.below_ceiling(0, 48) is None


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
