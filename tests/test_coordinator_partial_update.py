"""HA-coupled regression tests for #8 and #9.

The pure decision helpers are covered by tests/test_resilience.py. These drive
the real GatewayCoordinator against a fake GatewayClient, because the bug in #8
was not in a helper — it was in how the coordinator gathered its endpoints, and
only an end-to-end update exercises that.

    pip install pytest-homeassistant-custom-component
    pytest tests/test_coordinator_partial_update.py -q
"""

import logging

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wallbox_gateway import sensor as wb_sensor
from custom_components.wallbox_gateway.api import GatewayAuthError, GatewayUnreachable
from custom_components.wallbox_gateway.const import (
    DOMAIN,
    ENDPOINT_BOOT,
    ENDPOINT_CHARGE_LOG,
    ENDPOINT_CHARGER,
    ENDPOINT_DIAG,
    ENDPOINT_HEALTH,
    ENDPOINT_STATUS,
)
from custom_components.wallbox_gateway.coordinator import GatewayCoordinator
from custom_components.wallbox_gateway.resilience import OnceSeen

# A plausible-looking gateway. Status 1 = Charging.
GOOD = {
    ENDPOINT_STATUS: {"chg_sn": "SN123", "gw_fw": "3.2.9", "gen": 0},
    ENDPOINT_CHARGER: {
        "status": {"r": {"cp": 7.2, "en": 1234}},
        "realtime": {"r": {"charger_status": 1}},
    },
    ENDPOINT_DIAG: {"ble_reconnects": 0},
    ENDPOINT_HEALTH: {"temp_c": 39.0},
    ENDPOINT_BOOT: {"boot": 15},
    ENDPOINT_CHARGE_LOG: {"intervals": [{"start": 100, "end": 200}]},
}


class FakeClient:
    """Stands in for GatewayClient. `overrides` maps a path to an Exception."""

    base_url = "http://gateway.local"

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []

    async def get(self, path, timeout=6.0):
        self.calls.append((path, timeout))
        value = self.overrides.get(path, GOOD.get(path, {}))
        if isinstance(value, Exception):
            raise value
        return value

    async def bapi(self, met, par="null", wait_ms=5000):
        # BAPI reads are best-effort and already gathered with
        # return_exceptions=True; failing them keeps the test focused on HTTP.
        raise GatewayUnreachable("BLE asleep")


def _coordinator(hass, overrides=None, poll_interval=10):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Wallbox BLE",
        data={"host": "gateway.local", "poll_interval": poll_interval},
        options={},
    )
    entry.add_to_hass(hass)
    client = FakeClient(overrides)
    return GatewayCoordinator(hass, entry, client), client


def _paths(client):
    return [p for p, _ in client.calls]


# ── #8: a secondary endpoint failing must not fail the update ───────
async def test_charge_log_timeout_does_not_fail_the_update(hass):
    """The exact reported fault: /api/charge_log times out, everything dies."""
    err = GatewayUnreachable("timeout on /api/charge_log after 12s (TimeoutError)")
    coord, _ = _coordinator(hass, {ENDPOINT_CHARGE_LOG: err})

    data = await coord._async_update_data()

    # Pre-fix this raised UpdateFailed and every entity went unavailable.
    assert data["raw_status"]["chg_sn"] == "SN123"
    assert data["charger_realtime"]["charger_status"] == 1
    assert data["charger_status"]["cp"] == 7.2
    assert data["diag"] == {"ble_reconnects": 0}


async def test_failed_secondary_carries_prior_value_forward(hass):
    coord, client = _coordinator(hass)
    first = await coord._async_update_data()
    coord.data = first
    assert first["charge_log"] == [{"start": 100, "end": 200}]

    client.overrides[ENDPOINT_CHARGE_LOG] = GatewayUnreachable("timeout")
    client.overrides[ENDPOINT_DIAG] = GatewayUnreachable("timeout")
    coord._charge_log_every = 1  # force a charge_log attempt on this tick
    second = await coord._async_update_data()

    # Stale beats blank: the sensors hold their last reading rather than
    # flapping to unavailable/unknown.
    assert second["charge_log"] == [{"start": 100, "end": 200}]
    assert second["diag"] == {"ble_reconnects": 0}


@pytest.mark.parametrize("endpoint", [ENDPOINT_STATUS, ENDPOINT_CHARGER])
async def test_critical_endpoint_failure_still_fails_the_update(hass, endpoint):
    """Degrading must not become 'never report a real outage'. On a fresh
    coordinator (no prior data) a critical failure fails immediately."""
    coord, _ = _coordinator(hass, {endpoint: GatewayUnreachable("connection refused")})
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_transient_critical_stall_rides_through(hass):
    """A single critical-endpoint stall must NOT flap entities to unavailable
    once we have last-good data — it should be ridden through (the ~6s gateway
    stall a periodic charger event causes; forum report)."""
    coord, client = _coordinator(hass)
    first = await coord._async_update_data()
    coord.data = first  # HA normally sets this; do it explicitly for the test

    # One failed cycle: rides through, returns last-good, stays available.
    client.overrides[ENDPOINT_STATUS] = GatewayUnreachable("timeout on /api/status after 4s")
    second = await coord._async_update_data()
    assert second["raw_status"]["chg_sn"] == "SN123"  # last-good, not unavailable
    assert coord._critical_fail_streak == 1

    # Recovery clears the streak.
    del client.overrides[ENDPOINT_STATUS]
    third = await coord._async_update_data()
    assert coord._critical_fail_streak == 0
    assert third["raw_status"]["chg_sn"] == "SN123"


async def test_persistent_critical_failure_goes_unavailable_after_grace(hass):
    """A real outage must still surface: after the grace window of consecutive
    critical failures, the update fails and entities go unavailable."""
    coord, client = _coordinator(hass)
    coord.data = await coord._async_update_data()

    client.overrides[ENDPOINT_STATUS] = GatewayUnreachable("connection refused")
    # Grace is 3 → cycles 1 and 2 ride through, cycle 3 raises.
    await coord._async_update_data()   # streak 1, rides through
    await coord._async_update_data()   # streak 2, rides through
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()   # streak 3, unavailable


async def test_auth_failure_on_a_secondary_endpoint_still_triggers_reauth(hass):
    """Wrong credentials are wrong for every endpoint, so reauth must win
    over the degrade path even when only a secondary read reports it."""
    coord, _ = _coordinator(hass, {ENDPOINT_HEALTH: GatewayAuthError("401 on /api/health")})
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_charge_log_polls_on_its_own_cadence(hass):
    coord, client = _coordinator(hass, poll_interval=10)
    assert coord._charge_log_every == 30  # 300 s target / 10 s tick

    await coord._async_update_data()
    assert ENDPOINT_CHARGE_LOG in _paths(client)  # first cycle populates it

    client.calls.clear()
    for _ in range(29):  # cycles 2..30
        await coord._async_update_data()
    assert ENDPOINT_CHARGE_LOG not in _paths(client)
    # ...and /api/status was still read on every one of those ticks.
    assert _paths(client).count(ENDPOINT_STATUS) == 29

    client.calls.clear()
    await coord._async_update_data()  # cycle 31 — next charge_log slot
    assert ENDPOINT_CHARGE_LOG in _paths(client)


async def test_charge_log_gets_a_longer_timeout_than_the_rest(hass):
    coord, client = _coordinator(hass)
    await coord._async_update_data()
    timeouts = dict(client.calls)
    assert timeouts[ENDPOINT_CHARGE_LOG] > timeouts[ENDPOINT_STATUS]


async def test_repeated_endpoint_failure_logs_once(hass, caplog):
    coord, client = _coordinator(hass, {ENDPOINT_DIAG: GatewayUnreachable("timeout")})
    with caplog.at_level(logging.WARNING):
        for _ in range(50):
            await coord._async_update_data()
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "could not be read" in r.message]
    assert len(warnings) == 1, f"expected one warning, got {len(warnings)}"


# ── #9: an unmapped status code must not break the sensor ───────────
class FakeEntity:
    """Minimal stand-in for GatewaySensor — only what _status_label reads."""

    def __init__(self, code, zentri=False, gen=0):
        self._code = code
        self._zentri = zentri
        self._gen = gen
        self.coordinator = type("C", (), {"unknown_status_codes": OnceSeen()})()

    def _charger_status_code(self):
        return self._code

    def _is_zentri(self):
        return self._zentri

    def _status(self):
        return {"gen": self._gen}


def _options():
    """The exact option set the ENUM sensor declares."""
    for desc in wb_sensor.SENSORS:
        if desc.key == "charger_status":
            return set(desc.options)
    raise AssertionError("charger_status description not found")


def test_unmapped_code_reports_unknown_not_an_invalid_state():
    """Code 19 produced 'Code 19', which HA rejects on every state write."""
    entity = FakeEntity(19)
    assert wb_sensor._status_label(entity) is None
    assert wb_sensor._status_attrs(entity) == {"status_code": 19}


def test_no_status_code_can_produce_an_out_of_options_state():
    """The invariant the ENUM sensor depends on, over the whole code space."""
    options = _options()
    for code in list(range(-5, 256)) + [None]:
        for zentri in (False, True):
            for gen in (0, 1):
                label = wb_sensor._status_label(FakeEntity(code, zentri, gen))
                assert label is None or label in options, (code, zentri, gen, label)


def test_known_codes_still_map(hass):
    assert wb_sensor._status_label(FakeEntity(1)) == "Charging"
    assert wb_sensor._status_label(FakeEntity(4, gen=1)) == "Paused"
    assert wb_sensor._status_label(FakeEntity(4, gen=0)) == "Connected — not charging"


def test_unknown_code_warns_once_not_per_write(caplog):
    entity = FakeEntity(19)
    with caplog.at_level(logging.WARNING):
        for _ in range(3659):  # the reported flood
            wb_sensor._status_label(entity)
    warnings = [r for r in caplog.records if "does not map yet" in r.message]
    assert len(warnings) == 1


def test_each_distinct_unknown_code_warns_once(caplog):
    coord = type("C", (), {"unknown_status_codes": OnceSeen()})()
    with caplog.at_level(logging.WARNING):
        for code in (19, 19, 20, 19, 20, 21):
            e = FakeEntity(code)
            e.coordinator = coord
            wb_sensor._status_label(e)
    warnings = [r for r in caplog.records if "does not map yet" in r.message]
    assert len(warnings) == 3
