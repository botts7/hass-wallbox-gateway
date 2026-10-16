"""Unit tests for the periodic battery-balance schedule (pure, no HA)."""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import balance_schedule as b  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn


# ── clamp_interval ──────────────────────────────────────────────────
@case
def test_clamp_interval():
    assert b.clamp_interval(7) == 7
    assert b.clamp_interval(14) == 14
    assert b.clamp_interval(0) == b.MIN_INTERVAL_DAYS      # too small
    assert b.clamp_interval(9999) == b.MAX_INTERVAL_DAYS   # too big
    assert b.clamp_interval("bad") == b.DEFAULT_INTERVAL_DAYS
    assert b.clamp_interval(None) == b.DEFAULT_INTERVAL_DAYS


# ── _parse_date ─────────────────────────────────────────────────────
@case
def test_parse_date():
    assert b._parse_date("2026-10-06") == date(2026, 10, 6)
    assert b._parse_date(date(2026, 1, 2)) == date(2026, 1, 2)
    assert b._parse_date("2026-10-06T23:30:00+11:00") == date(2026, 10, 6)  # ISO datetime str
    assert b._parse_date(None) is None
    assert b._parse_date("") is None
    assert b._parse_date("garbage") is None


# ── is_balance_due ──────────────────────────────────────────────────
@case
def test_due_when_never_balanced():
    assert b.is_balance_due(None, date(2026, 10, 6), 7) is True


@case
def test_due_at_exactly_interval():
    # last balanced 7 days ago, weekly → due today
    assert b.is_balance_due("2026-09-29", date(2026, 10, 6), 7) is True


@case
def test_not_due_before_interval():
    # 6 days ago, weekly → not yet
    assert b.is_balance_due("2026-09-30", date(2026, 10, 6), 7) is False


@case
def test_overdue_still_due():
    assert b.is_balance_due("2026-09-01", date(2026, 10, 6), 7) is True


@case
def test_future_last_balance_not_due():
    # clock skew / manual edit: last is in the future → treat as not due
    assert b.is_balance_due("2026-10-20", date(2026, 10, 6), 7) is False


@case
def test_fortnightly():
    assert b.is_balance_due("2026-09-23", date(2026, 10, 6), 14) is False  # 13 days
    assert b.is_balance_due("2026-09-22", date(2026, 10, 6), 14) is True   # 14 days


@case
def test_balanced_today_not_due():
    assert b.is_balance_due("2026-10-06", date(2026, 10, 6), 7) is False


# ── next_balance_date ───────────────────────────────────────────────
@case
def test_next_balance_date():
    assert b.next_balance_date("2026-09-29", date(2026, 10, 1), 7) == date(2026, 10, 6)
    # never balanced → today
    assert b.next_balance_date(None, date(2026, 10, 6), 7) == date(2026, 10, 6)
    # overdue → today (don't schedule in the past)
    assert b.next_balance_date("2026-08-01", date(2026, 10, 6), 7) == date(2026, 10, 6)


# ── night_target_pct ────────────────────────────────────────────────
@case
def test_night_target_normal():
    tgt, is_bal = b.night_target_pct(53.0, balance_enabled=True, balance_due=False)
    assert tgt == 53.0 and is_bal is False


@case
def test_night_target_balance_night():
    tgt, is_bal = b.night_target_pct(53.0, balance_enabled=True, balance_due=True)
    assert tgt == 100.0 and is_bal is True


@case
def test_night_target_disabled():
    tgt, is_bal = b.night_target_pct(53.0, balance_enabled=False, balance_due=True)
    assert tgt == 53.0 and is_bal is False


@case
def test_night_target_never_below_floor():
    # a misconfigured balance target below the floor still charges the floor
    tgt, is_bal = b.night_target_pct(60.0, balance_enabled=True, balance_due=True,
                                     balance_target_pct=40.0)
    assert tgt == 60.0 and is_bal is True


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
