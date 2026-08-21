"""Unit tests for the allowed-window logic + composable config normalize
(pure, no Home Assistant)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import charge_window as w  # noqa: E402
import ca_config as cc  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn


def _m(h, mn=0):
    return h * 60 + mn


# ── to_minutes ──────────────────────────────────────────────────────
@case
def test_to_minutes():
    assert w.to_minutes("00:00") == 0
    assert w.to_minutes("06:30") == 390
    assert w.to_minutes("23:59") == 1439
    assert w.to_minutes("06:30:00") == 390      # HH:MM:SS tolerated
    assert w.to_minutes(None) is None
    assert w.to_minutes("nonsense") is None
    assert w.to_minutes("25:00") is None        # out of range


# ── in_window (incl. midnight wrap) ─────────────────────────────────
@case
def test_in_window_simple():
    assert w.in_window(_m(2), _m(0), _m(6)) is True
    assert w.in_window(_m(6), _m(0), _m(6)) is False   # end exclusive
    assert w.in_window(_m(8), _m(0), _m(6)) is False


@case
def test_in_window_wrap_midnight():
    # 23:00 → 07:00 spans midnight
    assert w.in_window(_m(23, 30), _m(23), _m(7)) is True
    assert w.in_window(_m(3), _m(23), _m(7)) is True
    assert w.in_window(_m(12), _m(23), _m(7)) is False


@case
def test_in_window_unset_is_always():
    assert w.in_window(_m(12), None, None) is True
    assert w.in_window(_m(12), _m(6), _m(6)) is True   # zero-length = always


# ── evaluate ────────────────────────────────────────────────────────
@case
def test_eval_no_window():
    r = w.evaluate(_m(14), start=None, end=None)
    assert r["allow_charge"] and r["reason"] == "no_window" and not r["cost_warn"]


@case
def test_eval_inside_window_allows_no_warn():
    r = w.evaluate(_m(2), start="00:00", end="06:00")
    assert r["in_window"] and r["allow_charge"]
    assert r["reason"] == "in_window" and r["cost_warn"] is False


@case
def test_eval_outside_blocks_by_default():
    r = w.evaluate(_m(14), start="00:00", end="06:00")
    assert r["allow_charge"] is False and r["reason"] == "outside_window"
    assert r["cost_warn"] is False     # not charging → no expensive charge


@case
def test_eval_target_met_never_charges_outside():
    r = w.evaluate(_m(14), start="00:00", end="06:00", overrun=True,
                   prestart=True, target_met=True,
                   minutes_to_departure=10, minutes_needed=120)
    assert r["allow_charge"] is False and r["reason"] == "target_met"


@case
def test_eval_overrun_continues_past_window():
    # 06:30, just past the 06:00 end, target not met, overrun on, ALREADY
    # charging → keep going past the window to finish the job.
    r = w.evaluate(_m(6, 30), start="00:00", end="06:00", overrun=True,
                   target_met=False, already_charging=True)
    assert r["allow_charge"] is True
    assert r["reason"] == "overrun_to_target" and r["cost_warn"] is True


@case
def test_eval_overrun_does_not_initiate_fresh_charge():
    # Same window/target but NOT already charging → overrun must NOT start a
    # fresh charge outside the cheap window (it only extends a running one).
    # Guards against a below-target battery starting at peak hours when a SOC
    # sensor blips, which let a full car charge at evening peak.
    r = w.evaluate(_m(18, 41), start="00:00", end="06:00", overrun=True,
                   target_met=False, already_charging=False)
    assert r["allow_charge"] is False and r["reason"] == "outside_window"


@case
def test_eval_overrun_off_blocks_outside():
    r = w.evaluate(_m(6, 30), start="00:00", end="06:00", overrun=False,
                   target_met=False)
    assert r["allow_charge"] is False and r["reason"] == "outside_window"


@case
def test_eval_prestart_for_departure():
    # 22:00, window 00:00-06:00 not yet open, but departure is close and we
    # need 120 min — must pre-start now (pricier → cost_warn).
    r = w.evaluate(_m(22), start="00:00", end="06:00", prestart=True,
                   target_met=False, minutes_to_departure=90, minutes_needed=120)
    assert r["allow_charge"] is True
    assert r["reason"] == "prestart_for_departure" and r["cost_warn"] is True


@case
def test_eval_prestart_not_yet_needed_blocks():
    # Plenty of time before departure → wait for the cheap window.
    r = w.evaluate(_m(22), start="00:00", end="06:00", prestart=True,
                   target_met=False, minutes_to_departure=600, minutes_needed=120)
    assert r["allow_charge"] is False and r["reason"] == "outside_window"


@case
def test_eval_prestart_takes_precedence_over_overrun():
    r = w.evaluate(_m(22), start="00:00", end="06:00", prestart=True, overrun=True,
                   target_met=False, minutes_to_departure=60, minutes_needed=120)
    assert r["reason"] == "prestart_for_departure"


# ── ca_config: composable normalize / legacy migration ──────────────
@case
def test_strategy_of():
    assert cc.strategy_of({"mode": "target_soc"}) == "target_soc"
    assert cc.strategy_of({"mode": "smart_solar"}) == "smart_solar"
    assert cc.strategy_of({"mode": "reminder"}) == "off"   # reminder is a layer now
    assert cc.strategy_of({}) == "off"
    assert cc.strategy_of(None) == "off"


@case
def test_reminder_config_legacy_flat():
    legacy = {"mode": "reminder", "triggers": ["nightly"], "nightly_time": "20:00",
              "notify_service": "notify.phone", "skip_above_pct": 80}
    rem = cc.reminder_config(legacy)
    assert rem["triggers"] == ["nightly"]
    assert rem["notify_service"] == "notify.phone"
    assert cc.reminder_enabled(legacy) is True


@case
def test_reminder_config_nested_layer():
    cfg = {"mode": "target_soc",
           "reminder": {"enabled": True, "triggers": ["arrival"],
                        "arrival_entity": "person.alex"}}
    rem = cc.reminder_config(cfg)
    assert rem["triggers"] == ["arrival"]
    assert cc.strategy_of(cfg) == "target_soc"      # layered on an acting strategy
    assert cc.reminder_enabled(cfg) is True


@case
def test_reminder_layer_disabled():
    cfg = {"mode": "solar", "reminder": {"enabled": False, "triggers": ["nightly"]}}
    assert cc.reminder_config(cfg) == {}
    assert cc.reminder_enabled(cfg) is False


@case
def test_reminder_absent():
    assert cc.reminder_config({"mode": "target_soc"}) == {}
    assert cc.reminder_enabled({"mode": "target_soc"}) is False


# ── hhmm_to_minutes (native-schedule "HHMM" clock values) ───────────
@case
def test_hhmm_to_minutes():
    assert w.hhmm_to_minutes(0) == 0
    assert w.hhmm_to_minutes(600) == 360         # 06:00
    assert w.hhmm_to_minutes("0600") == 360      # zero-padded string
    assert w.hhmm_to_minutes("600") == 360       # unpadded string
    assert w.hhmm_to_minutes(2330) == 1410       # 23:30
    assert w.hhmm_to_minutes(2359) == 1439
    assert w.hhmm_to_minutes(None) is None
    assert w.hhmm_to_minutes("") is None
    assert w.hhmm_to_minutes("nope") is None
    assert w.hhmm_to_minutes(2400) is None       # hour out of range
    assert w.hhmm_to_minutes(1270) is None       # minute out of range (70)


# ── overlaps (coexistence: does a schedule fall in the day window?) ──
@case
def test_overlaps_basic():
    # day window 09:00–16:00
    d0, d1 = _m(9), _m(16)
    assert w.overlaps(_m(10), _m(14), d0, d1) is True   # midday schedule overlaps
    assert w.overlaps(_m(0), _m(6), d0, d1) is False     # 00:00–06:00 night: no overlap
    assert w.overlaps(_m(16), _m(18), d0, d1) is False   # touches end — half-open, no overlap
    assert w.overlaps(_m(6), _m(9), d0, d1) is False     # touches start — no overlap
    assert w.overlaps(_m(15), _m(20), d0, d1) is True    # partial overlap at trailing edge


@case
def test_overlaps_midnight_wrap():
    assert w.overlaps(_m(23), _m(7), _m(9), _m(16)) is False   # 23:00–07:00 night vs day: no
    assert w.overlaps(_m(23), _m(10), _m(9), _m(16)) is True   # 23:00–10:00 clips into day
    assert w.overlaps(_m(2), _m(5), _m(22), _m(8)) is True     # charge inside a wrapping window
    assert w.overlaps(_m(23), _m(2), _m(22), _m(1)) is True    # two wrapping windows share pre-midnight


@case
def test_overlaps_degenerate():
    assert w.overlaps(None, _m(6), _m(9), _m(16)) is False     # unset endpoint
    assert w.overlaps(_m(0), _m(6), _m(9), None) is False
    assert w.overlaps(_m(6), _m(6), _m(0), _m(12)) is False    # zero-length covers nothing
    assert w.overlaps(_m(0), _m(12), _m(6), _m(6)) is False


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
