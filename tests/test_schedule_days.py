"""Unit tests for the set_schedule day encoding — the charger stores `days`
Sunday-first (bit0=Sun .. bit6=Sat), verified against the official Wallbox app
(Sunday=1, Mon-Fri=62). A Monday-first encoding wrote/read every day one earlier
(a Friday schedule landed on Thursday). See issue #11.

Needs HA importable (schedule.py pulls voluptuous/homeassistant); self-skips if
not, like the other controller tests."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

try:
    from wallbox_gateway import schedule as S
    _HA_OK = True
except Exception as e:  # pragma: no cover - environment without HA
    print(f"--- test_schedule_days: SKIPPED (HA not importable: {e})")
    _HA_OK = False

CASES = []
def case(fn):
    CASES.append(fn); return fn


def _mask(days):
    """7-element Sun..Sat bit-array -> the bitmask int the charger reads back."""
    return sum((1 if b else 0) << i for i, b in enumerate(days))


# ── write: _days_array (friendly names -> Sun-first bit array) ──────
@case
def test_friday_is_bit5_not_bit4():
    # The exact #11 repro: Friday must set bit 5 (Sun-first), not bit 4 (which
    # the charger reads as Thursday).
    arr = S._days_array(["fri"])
    assert arr == [0, 0, 0, 0, 0, 1, 0], arr
    assert _mask(arr) == 32


@case
def test_sunday_is_bit0():
    assert _mask(S._days_array(["sun"])) == 1


@case
def test_mon_fri_is_62():
    # Matches the Wallbox app (Mon-Fri = 62).
    assert _mask(S._days_array(["mon", "tue", "wed", "thu", "fri"])) == 62


@case
def test_all_days_is_127():
    assert _mask(S._days_array(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])) == 127


@case
def test_saturday_is_bit6():
    assert _mask(S._days_array(["sat"])) == 64


# ── read-back: _days_from_array (bitmask int AND bit array) ─────────
@case
def test_from_bitmask_mon_fri():
    assert set(S._days_from_array(62)) == {"mon", "tue", "wed", "thu", "fri"}


@case
def test_from_bitmask_sunday():
    assert S._days_from_array(1) == ["sun"]


@case
def test_from_bitmask_friday():
    assert S._days_from_array(32) == ["fri"]


@case
def test_from_bit_array():
    assert set(S._days_from_array([0, 0, 0, 0, 0, 1, 0])) == {"fri"}


# ── round-trip ─────────────────────────────────────────────────────
@case
def test_round_trip():
    for days in (["fri"], ["mon", "wed", "fri"], ["sun", "sat"],
                 ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]):
        back = S._days_from_array(_mask(S._days_array(days)))
        assert set(back) == {d.lower() for d in days}, (days, back)


def main():
    if not _HA_OK:
        print("  (skipped — HA not importable)")
        return
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
