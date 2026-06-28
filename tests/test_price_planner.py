"""Unit tests for the cheapest-window price planner (pure logic).

Runs standalone (no Home Assistant): price_planner imports only stdlib and
takes the datetime parser as a callable, so we import it directly and pass a
stub. Run:  py tests/test_price_planner.py   (also works under pytest).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# Import the module directly (avoid the package __init__ which pulls in HA).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import price_planner as pp  # noqa: E402

UTC = timezone.utc


def pdt(v):
    """Test stub mirroring dt_util.parse_datetime + as_utc."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def at(h, m=0):
    return datetime(2026, 6, 22, h, m, tzinfo=UTC)


def iso(h, m=0):
    return at(h, m).isoformat()


CASES = []
def case(fn):
    CASES.append(fn); return fn


# ── parse_forecast ──────────────────────────────────────────────────
@case
def test_parse_nordpool_raw_today_tomorrow():
    attrs = {
        "raw_today": [
            {"start": iso(0), "end": iso(1), "value": 0.30},
            {"start": iso(1), "end": iso(2), "value": 0.10},
        ],
        "raw_tomorrow": [
            {"start": iso(2), "end": iso(3), "value": 0.05},
        ],
    }
    slots = pp.parse_forecast(attrs, pdt, now=at(0))
    assert len(slots) == 3, slots
    assert [s.price for s in slots] == [0.30, 0.10, 0.05]
    assert slots[0].start == at(0) and slots[0].end == at(1)


@case
def test_parse_amber_forecasts_per_kwh_start_time():
    attrs = {"forecasts": [
        {"start_time": iso(10), "end_time": iso(10, 30), "per_kwh": 0.42},
        {"start_time": iso(10, 30), "end_time": iso(11), "per_kwh": 0.18},
    ]}
    slots = pp.parse_forecast(attrs, pdt, now=at(9))
    assert len(slots) == 2
    assert slots[1].price == 0.18
    assert (slots[0].end - slots[0].start) == timedelta(minutes=30)


@case
def test_parse_generic_infers_missing_end():
    attrs = {"forecast": [
        {"datetime": iso(1), "price": 0.2},
        {"datetime": iso(2), "price": 0.3},  # 60-min gap → end inferred
    ]}
    slots = pp.parse_forecast(attrs, pdt, now=at(0))
    assert slots[0].end == at(2)               # inferred from next start
    assert slots[1].end == at(3)               # last point → +60 min


@case
def test_parse_dedups_overlapping_today_tomorrow():
    attrs = {
        "raw_today": [{"start": iso(5), "end": iso(6), "value": 0.2}],
        "raw_tomorrow": [{"start": iso(5), "end": iso(6), "value": 0.2}],
    }
    assert len(pp.parse_forecast(attrs, pdt, now=at(0))) == 1


@case
def test_parse_drops_fully_past_slots():
    attrs = {"raw_today": [
        {"start": iso(0), "end": iso(1), "value": 0.2},   # past
        {"start": iso(5), "end": iso(6), "value": 0.2},   # future
    ]}
    slots = pp.parse_forecast(attrs, pdt, now=at(3))
    assert len(slots) == 1 and slots[0].start == at(5)


@case
def test_parse_bad_shapes_yield_empty():
    assert pp.parse_forecast(None, pdt, now=at(0)) == []
    assert pp.parse_forecast({}, pdt, now=at(0)) == []
    assert pp.parse_forecast({"raw_today": "nope"}, pdt, now=at(0)) == []
    assert pp.parse_forecast({"raw_today": [{"start": iso(1)}]}, pdt, now=at(0)) == []  # no price


# ── plan_cheapest ───────────────────────────────────────────────────
def _slots(*triples):
    return [pp.Slot(at(s), at(e), p) for (s, e, p) in triples]


@case
def test_plan_picks_cheapest_enough():
    # Four 1h slots; need 2h. Cheapest two are 02-03 (0.05) and 01-02 (0.10).
    slots = _slots((0, 1, 0.30), (1, 2, 0.10), (2, 3, 0.05), (3, 4, 0.40))
    chosen = pp.plan_cheapest(slots, energy_kwh=14.0, power_kw=7.0, now=at(0), deadline=at(8))
    starts = sorted(s.start.hour for s in chosen)
    assert starts == [1, 2], starts   # 2 hours, the two cheapest


@case
def test_plan_clips_to_now_and_deadline():
    slots = _slots((0, 4, 0.10))      # one 4h slot
    chosen = pp.plan_cheapest(slots, energy_kwh=7.0, power_kw=7.0, now=at(1), deadline=at(2, 30))
    assert len(chosen) == 1
    assert chosen[0].start == at(1) and chosen[0].end == at(2, 30)  # clipped both ends


@case
def test_plan_insufficient_window_returns_all_usable():
    # Need 5h but only 2h available before deadline → take everything.
    slots = _slots((0, 1, 0.50), (1, 2, 0.20))
    chosen = pp.plan_cheapest(slots, energy_kwh=35.0, power_kw=7.0, now=at(0), deadline=at(2))
    assert len(chosen) == 2


@case
def test_plan_zero_energy_or_bad_inputs():
    slots = _slots((0, 4, 0.1))
    assert pp.plan_cheapest(slots, 0, 7, at(0), at(4)) == []
    assert pp.plan_cheapest(slots, 7, 0, at(0), at(4)) == []
    assert pp.plan_cheapest(slots, 7, 7, at(4), at(0)) == []   # deadline before now


@case
def test_is_charge_now_and_next_window():
    chosen = _slots((1, 2, 0.1), (3, 4, 0.1))
    assert pp.is_charge_now(chosen, at(1, 30)) is True
    assert pp.is_charge_now(chosen, at(2, 30)) is False
    assert pp.is_charge_now(chosen, at(2)) is False           # end is exclusive
    nxt = pp.next_window(chosen, at(2, 30))
    assert nxt is not None and nxt.start == at(3)


@case
def test_plan_tiebreak_prefers_earlier():
    # Equal price → front-load (earlier slot chosen first).
    slots = _slots((5, 6, 0.10), (1, 2, 0.10), (3, 4, 0.10))
    chosen = pp.plan_cheapest(slots, energy_kwh=7.0, power_kw=7.0, now=at(0), deadline=at(8))
    assert len(chosen) == 1 and chosen[0].start == at(1)


def main():
    passed = 0
    for fn in CASES:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
