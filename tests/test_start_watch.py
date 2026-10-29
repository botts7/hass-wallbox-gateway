"""Start watcher (start_watch.py) and how the Charge Assistant acts on it.

Replays the real nights on a Pulsar MAX + BYD:
- 28 Oct: the charger sat in status 2 ("waiting for car"); the old watcher
  re-asserted at 12 s (a start in status 2 PAUSES a MAX session), the car never
  drew, it retried every 10 min and the charger went into Error.
- 27 Oct: status 2 for ~13 s, then charging.
Run:  py tests/test_start_watch.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "custom_components", "wallbox_gateway"))
import start_watch as W  # noqa: E402

sys.path.insert(0, HERE)
try:
    import test_controller_decisions as T   # reuse build() / FakeState
    _HA_OK = T._HA_OK
except Exception as e:  # pragma: no cover
    print(f"--- test_start_watch (assistant part): SKIPPED ({e})")
    _HA_OK = False

CASES = []
def case(fn):
    CASES.append(fn); return fn


# ── pure decisions ──────────────────────────────────────────────────
@case
def test_waiting_for_car_is_never_interrupted():
    # status 2 at checks 0..2 -> just wait; only the last check gives up.
    assert [W.outcome(2, False, i, False) for i in range(4)] == \
        [W.WAIT, W.WAIT, W.WAIT, W.GIVE_UP]
    assert [W.outcome(3, False, i, False) for i in range(4)] == \
        [W.WAIT, W.WAIT, W.WAIT, W.GIVE_UP]


@case
def test_done_when_charging():
    assert W.outcome(2, True, 0, False) == W.DONE    # power flowing beats the code
    assert W.outcome(1, False, 1, False) == W.DONE


@case
def test_taken_back_reasserts_once():
    assert W.outcome(18, False, 0, False, responded=True) == W.REASSERT
    assert W.outcome(18, False, 1, False) == W.REASSERT     # 30 s: data is post-start
    assert W.outcome(18, False, 1, True) == W.WAIT
    assert W.outcome(4, False, 0, False, responded=True) == W.REASSERT
    assert W.outcome(18, False, 3, True) == W.GIVE_UP


@case
def test_first_check_take_back_without_response_waits():
    # 29 Oct: at 12 s the cached status still said 18 (pre-start) while the
    # charger was already in 2. No sign the charger acted yet: just wait.
    assert W.outcome(18, False, 0, False) == W.WAIT
    assert W.outcome(4, False, 0, False) == W.WAIT


@case
def test_error_stops_at_once():
    assert W.outcome(7, False, 0, False) == W.FAULT


@case
def test_unknown_status_waits_then_gives_up():
    assert W.outcome(None, False, 0, False) == W.WAIT
    assert W.outcome(19, False, 3, False) == W.GIVE_UP


@case
def test_checks_cover_90_seconds():
    assert sum(W.CHECK_DELAYS_S) == 90 and W.CHECK_DELAYS_S[0] >= 12


# ── the assistant acting on it ──────────────────────────────────────
def _assistant(code, charging=False, status=None):
    C = T.C
    ca, calls = T.build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc",
                         C.CA_TARGET_PCT: 75}, {"sensor.soc": T.FakeState("63")})
    d = ca._coordinator().data
    d["charger_realtime"] = {"charger_status": code}
    d["raw_status"] = status or {"control_owner": "integration"}
    ca._is_charging = lambda: charging
    ca._we_started = True
    abandoned = []
    async def _abandon(c):
        abandoned.append(c)
    ca._abandon_start = _abandon
    created = []
    ca.hass.async_create_task = lambda coro: (created.append(coro), coro.close())
    ca._arm_start_check = lambda delay: None
    from homeassistant.util import dt as dt_util
    from datetime import timedelta
    started = dt_util.utcnow() - timedelta(seconds=12)
    ca._starting = {"reason": "test", "target": 75.0, "check": 0, "reasserted": False,
                    "at": started}
    ca._last_coord_update = dt_util.utcnow()    # data received after the start
    return ca, calls, abandoned, created


@case
def test_night_of_28_oct_no_command_until_give_up():
    if not _HA_OK:
        return
    ca, calls, _, created = _assistant(2)
    for _ in range(3):
        ca._verify_start()
        assert calls == [], f"must not send anything while waiting for the car: {calls}"
        assert ca._starting is not None
    ca._verify_start()                      # 4th check (90 s): give up
    assert ca._starting is None and len(created) == 1, "exactly one clean give-up"
    assert calls == []


@case
def test_27_oct_waiting_then_charging_is_success():
    if not _HA_OK:
        return
    ca, calls, _, created = _assistant(2)
    ca._verify_start()                      # 12 s: still waiting
    ca._is_charging = lambda: True
    ca._coordinator().data["charger_realtime"] = {"charger_status": 1}
    ca._verify_start()                      # 30 s: charging
    assert ca._starting is None and calls == [] and created == []


@case
def test_eco_take_back_reasserts_once_only():
    if not _HA_OK:
        return
    ca, calls, _, _ = _assistant(18)
    ca._is_paused = lambda: False
    ca._verify_start()
    assert calls == [], "12 s with no sign of the charger acting: wait"
    ca._verify_start()
    assert calls == [True], calls
    for _ in range(2):
        ca._verify_start()
    assert calls == [True], "only one re-assert"


@case
def test_error_status_abandons_without_commands():
    if not _HA_OK:
        return
    ca, calls, _, created = _assistant(7)
    ca._verify_start()
    assert ca._starting is None and calls == [] and len(created) == 1


@case
def test_no_auto_start_after_a_failed_start_until_unplug():
    if not _HA_OK:
        return
    ca, _, _, _ = _assistant(18)
    ca._starting = None
    ca._start_failed_this_plug = True
    assert ca._start_blocked()
    ca._plugged_in = lambda: False
    ca._track_plug_and_error()
    assert not ca._start_failed_this_plug and not ca._start_blocked()


@case
def test_error_blocks_starts_and_notifies_once():
    if not _HA_OK:
        return
    ca, _, _, created = _assistant(7)
    ca._starting = None
    assert ca._start_blocked()
    ca._track_plug_and_error()
    ca._track_plug_and_error()
    assert len(created) == 1, "one error notification per episode"


@case
def test_stale_pre_start_status_is_not_a_take_back():
    # Live test 28 Oct: at the 12 s check HA still held the pre-start status
    # ("queued", 18) and the watcher re-asserted. Status older than the start
    # must count as unknown: wait, never re-assert.
    if not _HA_OK:
        return
    ca, calls, _, created = _assistant(18)
    ca._last_coord_update = ca._starting["at"]  # nothing newer than the start
    ca._is_paused = lambda: False
    ca._verify_start()
    assert calls == [] and created == [] and ca._starting is not None, calls
    from homeassistant.util import dt as dt_util
    ca._last_coord_update = dt_util.utcnow()    # fresh data, still queued
    ca._verify_start()
    assert calls == [True], "a fresh 'queued' at 30 s is a real take-back"


@case
def test_29_oct_stale_r_sta_with_r_dat_waiting():
    # Night of 29 Oct, exact data at the 12 s check: r_sta (polled every 30 s)
    # still 18 from before the start, r_dat already 2. The watcher must read
    # r_dat, see the handshake, and send nothing.
    if not _HA_OK:
        return
    ca, calls, _, created = _assistant(18)
    ca._coordinator().data["charger_status"] = {"st": 2}
    ca._is_paused = lambda: True                  # control_mode 1 after our start
    ca._verify_start()
    assert calls == [] and created == [] and ca._starting is not None, calls
    ca._coordinator().data["charger_status"] = {"st": 1}
    ca._is_charging = lambda: True
    ca._verify_start()
    assert ca._starting is None and calls == []


@case
def test_take_back_after_charger_responded_reasserts_at_first_check():
    # A real Eco-Smart take-back seen right after the charger acted on the
    # start (2 seen, then a fresh 18) is still re-asserted, once.
    if not _HA_OK:
        return
    ca, calls, _, _ = _assistant(2)
    ca._is_paused = lambda: False
    ca._verify_start()                              # 12 s: waiting -> responded
    assert calls == []
    ca._coordinator().data["charger_realtime"] = {"charger_status": 18}
    ca._verify_start()
    assert calls == [True], calls


@case
def test_window_end_message_flags_a_stale_battery_reading():
    if not _HA_OK:
        return
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    C = T.C
    ca, _ = T.build({C.CA_MODE: C.MODE_TARGET, C.CA_SOC_ENTITY: "sensor.soc"},
                    {"sensor.soc": T.FakeState("67")})
    st = ca.hass.states.get("sensor.soc")
    st.last_updated = dt_util.utcnow() - timedelta(minutes=78)   # 04:42 -> 06:00
    msg = ca._window_end_message(67, 76)
    assert "about 78 min ago" in msg and "likely higher" in msg, msg
    st.last_updated = dt_util.utcnow() - timedelta(minutes=5)
    assert "paused at 67%" in ca._window_end_message(67, 76)


# ── the give-up itself, per charger family ──────────────────────────
class _Client:
    def __init__(self):
        self.sent = []
    async def command(self, payload):
        self.sent.append(payload.get("action"))
        return {"r": None}
    async def get(self, url):
        self.sent.append(url)
        return {}


def _giveup(code, status):
    C = T.C
    ca, _ = T.build({C.CA_MODE: C.MODE_TARGET}, {})
    coord = ca._coordinator()
    coord.client = _Client()
    coord.data["raw_status"] = status
    notes = []
    async def _note(kind, message_override=None):
        notes.append(message_override)
    ca._send_notification = _note
    import wallbox_gateway.charge_assistant as CA
    CA._ABANDON_SETTLE_S = 0
    ca._we_started = True
    ca._managed = True
    asyncio.new_event_loop().run_until_complete(ca._abandon_start(code))
    return ca, coord.client.sent, notes


@case
def test_giveup_max_waiting_stops_then_hands_back():
    if not _HA_OK:
        return
    ca, sent, notes = _giveup(2, {"control_owner": "integration", "chg_project": "prj20-pulsar-max-pro"})
    assert sent == ["stop", "resume"], sent
    assert not ca._we_started and not ca._managed and ca._start_failed_this_plug
    assert len(notes) == 1 and "replug" in notes[0]


@case
def test_giveup_never_stops_a_session_that_isnt_started():
    if not _HA_OK:
        return
    # Status 18 (queued): a stop there would TOGGLE charging on (MAX, and Plus
    # firmware that toggles), and older gateway firmware has no guard.
    _, sent, _ = _giveup(18, {"control_owner": "integration", "chg_project": "prj08-pulsar-plus"})
    assert sent == ["resume"], sent


@case
def test_giveup_original_pulsar_sends_nothing():
    if not _HA_OK:
        return
    _, sent, notes = _giveup(2, {"control_owner": "integration", "zentri": True})
    assert sent == [], sent
    assert len(notes) == 1


@case
def test_giveup_on_error_sends_nothing():
    if not _HA_OK:
        return
    _, sent, notes = _giveup(7, {"control_owner": "integration", "chg_project": "prj20-pulsar-max-pro"})
    assert sent == [] and "error" in notes[0].lower()


# ── the restart safety net needs a real stop -> idle -> charging ────
@case
def test_adopt_ignores_status_lag_after_our_stop():
    if not _HA_OK:
        return
    from homeassistant.util import dt as dt_util
    C = T.C
    ca, _ = T.build({C.CA_MODE: C.MODE_TARGET}, {})
    ca._coordinator().data["raw_status"] = {"control_owner": "integration",
                                            "last_command_by": "integration"}
    ca._we_started = False
    ca._last_own_stop = dt_util.utcnow()
    ca._seen_idle_after_own_stop = False
    ca._is_charging = lambda: True          # HA still shows charging (lag)
    ca._maybe_adopt_restart()
    assert ca._we_started is False, "status lag right after our stop is not a restart"
    ca._is_charging = lambda: False
    ca._track_plug_and_error()              # charger seen idle
    ca._is_charging = lambda: True          # ...then charging again: a real restart
    ca._maybe_adopt_restart()
    assert ca._we_started is True


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
