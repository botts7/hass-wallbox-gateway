"""Unit tests for the graceful-degradation helpers (pure, no Home Assistant).

Covers the decision cores behind #8 (a secondary endpoint timing out must not
take the whole device unavailable) and #9 (an unmapped charger status code must
degrade to `unknown` and be logged once, not on every state write).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import resilience as r  # noqa: E402

CASES = []
def case(fn):
    CASES.append(fn); return fn


# Mirrors of the real tables — kept local so the tests stay HA-free and don't
# break when a new code is added to const.STATUS_CODES.
STATUS_CODES = {
    0: "Ready",
    1: "Charging",
    2: "Connected — waiting for car",
    4: "Paused",
    7: "Error",
}
ZENTRI_CODES = {0: "Ready", 1: "Charging", 4: "Charging"}
NOT_CHARGING = "Connected — not charging"


class Boom(Exception):
    pass


class Auth(Exception):
    pass


# ── partition_results (#8) ──────────────────────────────────────────
@case
def test_partition_all_ok():
    ok, failed = r.partition_results(["a", "b"], [1, 2])
    assert ok == {"a": 1, "b": 2}
    assert failed == {}


@case
def test_partition_splits_failures_from_successes():
    err = Boom("timeout on /api/charge_log")
    ok, failed = r.partition_results(
        ["status", "charger", "charge_log"], [{"s": 1}, {"c": 2}, err]
    )
    # The whole point of #8: the two successes survive the third's failure.
    assert ok == {"status": {"s": 1}, "charger": {"c": 2}}
    assert failed == {"charge_log": err}


@case
def test_partition_keeps_falsy_successes():
    # An endpoint legitimately answering {} / None / [] is a success, not a
    # failure — `or`-style truthiness checks would misclassify these.
    ok, failed = r.partition_results(["a", "b", "c"], [{}, None, []])
    assert failed == {}
    assert set(ok) == {"a", "b", "c"}


@case
def test_partition_ignores_extra_names():
    # A skipped slow-cadence endpoint contributes no result at all.
    ok, failed = r.partition_results(["a", "b"], [1])
    assert ok == {"a": 1}
    assert failed == {}


# ── first_exception (#8) ────────────────────────────────────────────
@case
def test_first_exception_finds_matching_type():
    auth = Auth("401")
    failed = {"status": Boom("x"), "health": auth}
    assert r.first_exception(failed, Auth) is auth


@case
def test_first_exception_returns_none_when_absent():
    assert r.first_exception({"a": Boom("x")}, Auth) is None


@case
def test_first_exception_accepts_tuple_of_types():
    boom = Boom("x")
    assert r.first_exception({"a": boom}, (Auth, Boom)) is boom


@case
def test_cancellation_is_visible_not_swallowed():
    # gather(return_exceptions=True) also captures CancelledError, which is a
    # BaseException and means HA is tearing the entry down — the coordinator
    # must be able to spot it and re-raise rather than "degrade".
    import asyncio
    cancel = asyncio.CancelledError()
    ok, failed = r.partition_results(["status", "health"], [{"s": 1}, cancel])
    assert failed["health"] is cancel
    assert r.first_exception(failed, asyncio.CancelledError) is cancel


# ── due / cycles_for (#8) ───────────────────────────────────────────
@case
def test_due_runs_on_first_cycle():
    # Entities must populate at startup rather than waiting a full interval.
    assert r.due(1, 30) is True


@case
def test_due_respects_cadence():
    assert [c for c in range(1, 63) if r.due(c, 30)] == [1, 31, 61]


@case
def test_due_every_one_is_always():
    assert all(r.due(c, 1) for c in range(1, 10))
    assert all(r.due(c, 0) for c in range(1, 10))


@case
def test_cycles_for_anchors_to_wall_clock():
    assert r.cycles_for(300, 10) == 30      # 10 s tick  → every 5 min
    assert r.cycles_for(300, 60) == 5       # 60 s tick  → every 5 min
    assert r.cycles_for(300, 600) == 1      # slower tick than target → every tick
    assert r.cycles_for(300, 0) == 1        # guard against a bad interval


# ── OnceSeen (#8, #9) ───────────────────────────────────────────────
@case
def test_once_seen_fires_once_per_key():
    seen = r.OnceSeen()
    assert seen.is_new(19) is True
    assert seen.is_new(19) is False
    assert seen.is_new(19) is False
    assert seen.is_new(20) is True


@case
def test_once_seen_scales_to_the_reported_flood():
    # 3,659 identical writes produced 3,659 tracebacks in #9. One log line now.
    seen = r.OnceSeen()
    fired = sum(1 for _ in range(3659) if seen.is_new("Code 19"))
    assert fired == 1


@case
def test_once_seen_forget_allows_refire():
    seen = r.OnceSeen()
    seen.is_new("a")
    seen.forget("a")
    assert seen.is_new("a") is True


@case
def test_once_seen_membership_and_len():
    seen = r.OnceSeen()
    seen.is_new("a")
    assert "a" in seen and len(seen) == 1


# ── resolve_status_label (#9) ───────────────────────────────────────
def _resolve(code, zentri=False, gen=0):
    return r.resolve_status_label(
        code,
        zentri=zentri,
        gen=gen,
        status_codes=STATUS_CODES,
        zentri_codes=ZENTRI_CODES,
        not_charging_label=NOT_CHARGING,
    )


@case
def test_known_code_maps_to_label():
    assert _resolve(1) == ("Charging", None)


@case
def test_unknown_code_degrades_to_none():
    # Regression for #9: the old code returned "Code 19", which is not in the
    # sensor's `options`, so HA raised ValueError on every state write.
    label, unknown = _resolve(19)
    assert label is None
    assert unknown == 19


@case
def test_every_returned_label_is_a_valid_enum_option():
    # The contract the ENUM sensor depends on: whatever comes back is either
    # None or a member of the declared option set, for any input at all.
    options = set(STATUS_CODES.values()) | {NOT_CHARGING}
    for code in list(range(-5, 60)) + [None]:
        for zentri in (False, True):
            for gen in (0, 1):
                label, _ = _resolve(code, zentri=zentri, gen=gen)
                assert label is None or label in options, (code, zentri, gen, label)


@case
def test_none_code_is_not_reported_as_unknown():
    # No reading yet is not the same as an unmapped code — it must not warn.
    assert _resolve(None) == (None, None)


@case
def test_status_4_disambiguates_paused_from_idle():
    assert _resolve(4, gen=1) == ("Paused", None)
    assert _resolve(4, gen=0) == (NOT_CHARGING, None)
    assert _resolve(4, gen=None) == (NOT_CHARGING, None)


@case
def test_zentri_uses_its_own_table_for_status_4():
    # Zentri st4 is the charge ramp, not "Paused" — and must not take the
    # gen-based disambiguation path.
    assert _resolve(4, zentri=True, gen=0) == ("Charging", None)


@case
def test_zentri_falls_back_to_max_table():
    # Zentri doesn't define 7; the MAX table does, and that behaviour predates
    # this change.
    assert _resolve(7, zentri=True) == ("Error", None)


@case
def test_zentri_unknown_still_degrades():
    assert _resolve(19, zentri=True) == (None, 19)


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
