"""A charge the assistant started survives an HA restart (own_charge.py).

Replays the 29 Oct live test: HA restarted at 20:35:26, 26 s after the window
end and before the next check. The assistant came back not knowing the charge
was its own and never stopped it.
Run:  py tests/test_own_charge.py
"""

import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "custom_components", "wallbox_gateway"))
import own_charge as O  # noqa: E402

sys.path.insert(0, HERE)
try:
    import test_controller_decisions as T   # reuse build() / FakeState
    _HA_OK = T._HA_OK
except Exception as e:  # pragma: no cover
    print(f"--- test_own_charge (assistant part): SKIPPED ({e})")
    _HA_OK = False

CASES = []
def case(fn):
    CASES.append(fn); return fn


NOW = 1_800_000_000.0
OURS = dict(now_s=NOW, loaded=True, charging=True, plugged=True,
            last_by="integration", manual=True, usid=2443500)


def _d(saved, **kw):
    args = {**OURS, **kw}
    return O.decide(saved, **args)


# ── pure rules ──────────────────────────────────────────────────────
@case
def test_29_oct_restart_mid_charge_is_ours():
    assert _d(O.record(NOW - 1040)) == O.KEEP               # started 20:18, restart 20:35
    assert _d({**O.record(NOW - 1040), "usid": 2443500}) == O.KEEP


@case
def test_waits_until_charger_data_is_loaded():
    assert _d(O.record(NOW - 60), loaded=False) == O.WAIT


@case
def test_no_record_or_bad_record_is_dropped():
    assert _d(None) == O.DROP
    assert _d({"at": "x"}) == O.DROP
    assert _d(O.record(NOW - O.MAX_AGE_S - 1)) == O.DROP    # too old
    assert _d(O.record(NOW + 600)) == O.DROP                # from the future


@case
def test_not_charging_or_unplugged_is_dropped():
    assert _d(O.record(NOW - 60), charging=False) == O.DROP
    assert _d(O.record(NOW - 60), plugged=False) == O.DROP
    assert _d(O.record(NOW - 60), plugged=None) == O.DROP


@case
def test_someone_elses_charge_is_left_alone():
    # A person or automation commanded the gateway since.
    assert _d(O.record(NOW - 60), last_by="manual") == O.DROP
    # Eco-Smart's own charging / after our hand-back: charger not in manual.
    assert _d(O.record(NOW - 60), manual=False) == O.DROP
    # A different session from the one we started.
    assert _d({**O.record(NOW - 60), "usid": 1}) == O.DROP


# ── the assistant acting on it ──────────────────────────────────────
def _restarted_assistant(saved, *, charging=True, paused=True, last_by="integration",
                         usid=2443500, loaded=True):
    C = T.C
    ca, calls = T.build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                         C.CA_TARGET_PCT: 78}, {"sensor.soc": T.FakeState("54")})
    d = ca._coordinator().data
    if loaded:
        d["raw_status"] = {"control_owner": "integration", "last_command_by": last_by}
        d["charger_status"] = {"st": 1 if charging else 18, "usid": usid}
    else:
        d["raw_status"] = {}
    ca._is_charging = lambda: charging
    ca._is_paused = lambda: paused
    ca._restored_own = saved          # what async_start loads from the Store
    ca._own_record = saved
    return ca, calls


@case
def test_assistant_reclaims_and_window_end_stops_it():
    if not _HA_OK:
        return
    from homeassistant.util import dt as dt_util
    ca, _ = _restarted_assistant(O.record(dt_util.utcnow().timestamp() - 1040))
    assert ca._we_started is False
    ca._maybe_reclaim_after_restart()
    assert ca._we_started is True and ca._managed is True and ca._restored_own is None
    stopped = []
    ca._finish_charge = lambda *a, **k: stopped.append(k.get("message"))
    ca._window_decision = lambda soc, target: {"in_window": False, "allow_charge": False,
                                               "reason": "outside_window", "cost_warn": False}
    ca._eval_target_windowed(54.0, 78.0, True)
    assert len(stopped) == 1, "the window-end stop must happen after the restart"


@case
def test_assistant_waits_for_data_then_decides():
    if not _HA_OK:
        return
    from homeassistant.util import dt as dt_util
    saved = O.record(dt_util.utcnow().timestamp() - 60)
    ca, _ = _restarted_assistant(saved, loaded=False)
    ca._maybe_reclaim_after_restart()
    assert ca._restored_own == saved and ca._we_started is False


@case
def test_assistant_leaves_eco_charge_alone_and_forgets_record():
    if not _HA_OK:
        return
    from homeassistant.util import dt as dt_util
    ca, _ = _restarted_assistant(O.record(dt_util.utcnow().timestamp() - 60), paused=False)
    ca._maybe_reclaim_after_restart()
    assert ca._we_started is False and ca._restored_own is None and ca._own_record is None


@case
def test_record_saved_on_start_cleared_on_stop_and_session_noted():
    if not _HA_OK:
        return
    import wallbox_gateway.charge_assistant as CA
    C = T.C
    ca, _ = T.build({C.CA_MODE: C.MODE_TARGET}, {})
    ca._set_charging = CA.ChargeAssistant._set_charging.__get__(ca)   # the real one
    ca._send_command = lambda *a, **k: _noop()
    ca._set_charging(True)
    assert ca._we_started and ca._own_record and ca._own_record["usid"] is None
    ca._coordinator().data["charger_status"] = {"st": 1, "usid": 77}
    ca._is_charging = lambda: True
    ca._note_own_session()
    assert ca._own_record["usid"] == 77
    ca._set_charging(False)
    assert ca._own_record is None and not ca._we_started


async def _noop():
    return None


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
