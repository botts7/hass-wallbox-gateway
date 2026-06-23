"""Equivalence tests for the Python cost engine — same scenarios verified
in-browser against cost.js, so a pass proves the two engines agree (no
soft-regression / divergent numbers). No Home Assistant needed.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import cost_engine as ce  # noqa: E402

UTC = timezone.utc

# TOU tariff: peak 17:00–20:59 @ 0.45, everything else off-peak @ 0.18.
WD = {h: ("pk" if 17 <= h < 21 else "off") for h in range(24)}
TARIFF = {"type": "tou", "weekendSame": True,
          "bands": [{"id": "off", "rate": 0.18}, {"id": "pk", "rate": 0.45}],
          "weekday": WD}

PLUG = int(datetime(2026, 6, 24, 18, 0, tzinfo=UTC).timestamp())   # 18:00 (peak)
CHG = int(datetime(2026, 6, 25, 2, 0, tzinfo=UTC).timestamp())     # 02:00 (off-peak)
SESSION = {"ts": PLUG, "stop": PLUG + 12 * 3600, "en": 10000, "gen": 0, "dur": 7200}
LOG = [{"start": CHG, "stop": CHG + 7200, "wh": 10000, "gwh": 0}]

CASES = []
def case(fn):
    CASES.append(fn); return fn


def approx(a, b, eps=0.001):
    return abs(a - b) < eps


@case
def test_actual_cost_offpeak_window():
    c = ce.session_cost(TARIFF, SESSION, LOG, "UTC")
    assert approx(c["total"], 1.80), c["total"]      # 10 kWh @ 0.18


@case
def test_baseline_plug_in_peak():
    b = ce.baseline_cost(TARIFF, SESSION, LOG, "UTC", {"mode": "plug_in"})
    assert approx(b, 4.50), b                          # 10 kWh @ 0.45


@case
def test_baseline_flat_avg():
    b = ce.baseline_cost(TARIFF, SESSION, LOG, "UTC", {"mode": "flat_avg"})
    assert approx(b, 2.25), b                          # (4*0.45 + 20*0.18)/24 * 10


@case
def test_baseline_fixed_midnight_and_6pm():
    b0 = ce.baseline_cost(TARIFF, SESSION, LOG, "UTC", {"mode": "fixed_time", "fixedTime": "00:00"})
    b18 = ce.baseline_cost(TARIFF, SESSION, LOG, "UTC", {"mode": "fixed_time", "fixedTime": "18:00"})
    assert approx(b0, 1.80), b0                        # off-peak
    assert approx(b18, 4.50), b18                      # peak


@case
def test_savings_shift():
    shift, solar = ce.session_savings(TARIFF, SESSION, LOG, "UTC", {"mode": "plug_in"})
    assert approx(shift, 2.70), shift                  # 4.50 − 1.80
    assert approx(solar, 0.0), solar


@case
def test_flat_tariff_no_shift_savings():
    flat = {"type": "flat", "flatRate": 0.30}
    c = ce.session_cost(flat, SESSION, LOG, "UTC")
    b = ce.baseline_cost(flat, SESSION, LOG, "UTC", {"mode": "plug_in"})
    assert approx(c["total"], 3.0), c["total"]         # 10 kWh @ 0.30
    assert approx(b, 3.0), b                            # flat → identical → no shift saving


@case
def test_solar_saved_value():
    # 10 kWh charged, 4 kWh of it green → grid 6 kWh; saved = 4 kWh × rate.
    s = {"ts": PLUG, "stop": PLUG + 12 * 3600, "en": 10000, "gen": 4000, "dur": 7200}
    log = [{"start": CHG, "stop": CHG + 7200, "wh": 10000, "gwh": 4000}]
    c = ce.session_cost(TARIFF, s, log, "UTC")
    assert approx(c["green"], 4.0), c["green"]
    assert approx(c["saved"], 4.0 * 0.18), c["saved"]  # green valued at the off-peak band it ran in
    assert approx(c["total"], 6.0 * 0.18), c["total"]  # only grid 6 kWh billed


@case
def test_summarize_cost_window():
    now = CHG + 3600   # 1h after the off-peak burst
    summary = ce.summarize_cost(TARIFF, LOG, "UTC", now)
    assert approx(summary["week_cost"], 1.80), summary
    assert approx(summary["month_cost"], 1.80), summary
    assert summary["currency"] == "$"


@case
def test_summarize_cost_excludes_old_bursts():
    old = {"start": CHG - 40 * 86400, "stop": CHG - 40 * 86400 + 7200, "wh": 10000, "gwh": 0}
    now = CHG + 3600
    summary = ce.summarize_cost(TARIFF, [LOG[0], old], "UTC", now)
    assert approx(summary["week_cost"], 1.80), summary   # old burst excluded from week


@case
def test_summarize_cost_no_tariff():
    assert ce.summarize_cost(None, LOG, "UTC", CHG + 3600) is None


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
