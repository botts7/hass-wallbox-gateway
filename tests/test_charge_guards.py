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


# ── pause_from_status (manual-override detection) ───────────────────
@case
def test_pause_prefers_schedule_paused():
    # Authoritative boolean wins regardless of gen.
    assert g.pause_from_status({"schedule_paused": True, "gen": 0}) is True
    assert g.pause_from_status({"schedule_paused": False, "gen": 1234}) is False


@case
def test_pause_max_pro_green_energy_not_a_flag():
    # The MAX Pro bug: gen is green energy. With the authoritative flag present
    # and False, a non-zero gen must NOT read as paused.
    assert g.pause_from_status({"schedule_paused": False, "gen": 5000}) is False


@case
def test_pause_legacy_gen_fallback():
    # Old gateway build with no schedule_paused field → fall back to gen.
    assert g.pause_from_status({"gen": 1}) is True
    assert g.pause_from_status({"gen": 0}) is False
    assert g.pause_from_status({}, {"gen": 2}) is True   # gen on realtime
    assert g.pause_from_status({}) is False


@case
def test_pause_degenerate_inputs():
    assert g.pause_from_status(None) is False
    assert g.pause_from_status({"gen": None}) is False
    assert g.pause_from_status({"schedule_paused": "yes"}) is False  # non-bool ignored → no gen → False


# ── #67: auto-resume must not undo a deliberate stop ──────────────────
@case
def test_native_resume_nothing_to_hand_back():
    # Reporters' setups: Eco-Smart disabled, no schedules -> nothing native.
    assert g.has_native_to_resume(0, []) is False
    assert g.has_native_to_resume(0, [{"sid": 0, "enabled": 0}]) is False
    assert g.has_native_to_resume(None, []) is False   # no Eco-Smart at all


@case
def test_native_resume_eco_or_schedule():
    assert g.has_native_to_resume(1, []) is True        # Full Green
    assert g.has_native_to_resume(2, []) is True        # Eco-Smart
    assert g.has_native_to_resume(0, [{"enabled": 1}]) is True
    assert g.has_native_to_resume(0, [{"enabled": 0}, {"enabled": True}]) is True


@case
def test_native_resume_unknown_keeps_old_behaviour():
    assert g.has_native_to_resume(0, None) is True      # schedules not read yet
    assert g.has_native_to_resume(None, None) is True
    assert g.has_native_to_resume(True, [])  is False   # bool is not a mode


@case
def test_user_stop_hold_window():
    h = g.USER_STOP_HOLD_S
    assert g.user_stop_holds(None, 1000) is False
    assert g.user_stop_holds(1000, 1000) is True
    assert g.user_stop_holds(1000, 1000 + h - 1) is True
    assert g.user_stop_holds(1000, 1000 + h) is False
    assert g.user_stop_holds(1000, 999) is False         # clock went backwards


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
