"""Unit tests for the charger control adapter (pure, no Home Assistant).

charger_control imports only stdlib, so we load it directly and drive it with a
fake coordinator/client that records the payloads it would send over BLE.
Run:  py tests/test_charger_control.py   (also works under pytest).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "wallbox_gateway"))
import charger_control as cc  # noqa: E402


class FakeClient:
    def __init__(self):
        self.sent = []
    async def command(self, payload):
        self.sent.append(payload)
        return {"r": None}


class FakeCoord:
    def __init__(self, data=None):
        self.client = FakeClient()
        self.data = data or {}


CASES = []
def case(fn):
    CASES.append(fn); return fn


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@case
def test_start_payload():
    c = FakeCoord()
    run(cc.WallboxGatewayCharger(c).start())
    assert c.client.sent == [{"action": "start", "owner": "integration", "wait": "5000"}], c.client.sent


@case
def test_stop_payload():
    c = FakeCoord()
    run(cc.WallboxGatewayCharger(c).stop())
    assert c.client.sent[0]["action"] == "stop"
    assert c.client.sent[0]["owner"] == "integration"


@case
def test_set_current_payload_and_clamp():
    c = FakeCoord()
    ch = cc.WallboxGatewayCharger(c)
    run(ch.set_current(16))
    assert c.client.sent[-1] == {"action": "current", "owner": "integration", "wait": "5000", "value": "16"}
    run(ch.set_current(99))    # clamp high → 32
    assert c.client.sent[-1]["value"] == "32"
    run(ch.set_current(1))     # clamp low → 6
    assert c.client.sent[-1]["value"] == "6"


@case
def test_capabilities_plus_max():
    c = FakeCoord({"raw_status": {"zentri": False, "meter": True, "chg_project": "prj20-pulsar-max-pro"}})
    caps = cc.WallboxGatewayCharger(c).capabilities()
    assert caps.can_set_current is True
    assert caps.has_meter is True
    assert caps.model == "prj20-pulsar-max-pro"
    assert (caps.min_current, caps.max_current) == (6, 32)


@case
def test_capabilities_zentri_blocks_current():
    c = FakeCoord({"raw_status": {"zentri": True, "meter": False, "dev_model": "NINA-B22"}})
    caps = cc.WallboxGatewayCharger(c).capabilities()
    assert caps.can_set_current is False     # original Pulsar — no live current
    assert caps.has_meter is False


@case
def test_state_reads():
    c = FakeCoord({
        "raw_status": {"control_owner": "integration", "last_command_by": "integration", "last_command_age_s": 7},
        "meter": {"house_power_w": 865},
    })
    ch = cc.WallboxGatewayCharger(c)
    assert ch.control_owner() == "integration"
    assert ch.last_command() == ("integration", 7)
    assert ch.house_power_w() == 865.0


@case
def test_state_reads_empty_coordinator():
    ch = cc.WallboxGatewayCharger(None)
    assert ch.control_owner() == ""
    assert ch.last_command() == ("", -1)
    assert ch.house_power_w() is None


def main():
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
