"""Unit tests for the Phase 4 charge guards (pure, no Home Assistant)."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import charge_guards as g  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)

CASES = []
def case(fn):
    CASES.append(fn); return fn


# ── effective_target (battery care) ─────────────────────────────────
@case
def test_daily_when_no_trip():
    assert g.effective_target(80, None, None, NOW) == 80.0


@case
def test_trip_target_applies_before_deadline():
    until = NOW + timedelta(hours=6)
    assert g.effective_target(80, 100, until, NOW) == 100.0


@case
def test_trip_target_expires_after_deadline():
    until = NOW - timedelta(hours=1)        # already passed
    assert g.effective_target(80, 100, until, NOW) == 80.0


@case
def test_trip_never_lowers_below_daily():
    until = NOW + timedelta(hours=6)
    # A trip target below the daily care target must not reduce charging.
    assert g.effective_target(80, 70, until, NOW) == 80.0


@case
def test_bad_inputs_fall_back_to_daily():
    # Bad daily + no trip → default daily (80).
    assert g.effective_target("oops", None, None, NOW) == 80.0
    # Valid daily + non-numeric trip target → ignore trip, keep daily.
    assert g.effective_target(80, "x", NOW + timedelta(hours=1), NOW) == 80.0


# ── price_allows_charge (cost cap) ──────────────────────────────────
@case
def test_no_cap_always_allows():
    assert g.price_allows_charge(0.99, None) is True
    assert g.price_allows_charge(0.99, "") is True


@case
def test_below_cap_allows_at_or_under():
    assert g.price_allows_charge(0.20, 0.30) is True
    assert g.price_allows_charge(0.30, 0.30) is True     # boundary inclusive


@case
def test_above_cap_blocks():
    assert g.price_allows_charge(0.45, 0.30) is False


@case
def test_unknown_price_fails_open():
    assert g.price_allows_charge(None, 0.30) is True
    assert g.price_allows_charge("unavailable", 0.30) is True


# ── derive_surplus (surplus-source wizard) ──────────────────────────
@case
def test_surplus_entity_passthrough():
    assert g.derive_surplus("entity", surplus=2100) == 2100.0
    assert g.derive_surplus("entity", surplus=None) is None
    assert g.derive_surplus("entity", surplus="unavailable") is None


@case
def test_surplus_from_grid_export_negative():
    # Exporting 1500 W → grid reads -1500 → surplus 1500.
    assert g.derive_surplus("grid", grid=-1500, grid_export_negative=True) == 1500.0
    # Importing (grid +800) → no surplus.
    assert g.derive_surplus("grid", grid=800, grid_export_negative=True) == 0.0


@case
def test_surplus_from_grid_export_positive_convention():
    # A dedicated export sensor where positive = exporting.
    assert g.derive_surplus("grid", grid=1500, grid_export_negative=False) == 1500.0
    assert g.derive_surplus("grid", grid=-200, grid_export_negative=False) == 0.0


@case
def test_surplus_from_solar_minus_load():
    assert g.derive_surplus("solar_load", solar=4000, load=1500) == 2500.0
    assert g.derive_surplus("solar_load", solar=1000, load=1500) == 0.0   # clamped
    assert g.derive_surplus("solar_load", solar=4000, load=None) is None  # missing input


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
