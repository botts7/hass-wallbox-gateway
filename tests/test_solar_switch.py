"""Unit tests for the 'Solar charging' switch (Feature A).

Focused, HA-light: we bypass CoordinatorEntity.__init__ (via __new__) and drive
the switch's value_fn / turn_on / turn_off against a fake coordinator whose
`client.bapi` records the s_ecos writes. Self-skips when HA isn't importable.
Run:  py tests/test_solar_switch.py
"""

import asyncio
import json
import os
import sys

# Import the integration as a package (custom_components on path) so the
# package's relative imports resolve and stdlib `select` isn't shadowed.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))
import select  # noqa: F401,E402  (preload before the package shadows it)

try:
    from wallbox_gateway import switch as sw_mod
    _HA_OK = True
except Exception as e:  # pragma: no cover - environment without HA
    print(f"--- test_solar_switch: SKIPPED (HA not importable: {e})")
    _HA_OK = False

CASES = []
def case(fn):
    CASES.append(fn); return fn


class FakeClient:
    def __init__(self):
        self.calls = []
    async def bapi(self, met, par=None, wait_ms=None):
        self.calls.append((met, par))


class FakeCoord:
    def __init__(self, eco):
        self.data = {} if eco is None else {"eco_smart": eco}
        self.client = FakeClient()
        self.refreshes = 0
    async def async_request_refresh(self):
        self.refreshes += 1


def _solar_desc():
    for d in sw_mod.SWITCHES:
        if d.key == "solar_charging":
            return d
    raise AssertionError("solar_charging switch description not found")


def _make(eco):
    """A GatewaySwitch wired to a FakeCoord, without running the HA base
    __init__ (which needs a real coordinator)."""
    coord = FakeCoord(eco)
    s = sw_mod.GatewaySwitch.__new__(sw_mod.GatewaySwitch)
    s.coordinator = coord
    s.entity_description = _solar_desc()
    return s, coord


# ── is_on across eco modes ──────────────────────────────────────────
@case
def test_is_on_reflects_eco_mode():
    assert _make({"mode": 0})[0].is_on is False, "Disabled → off"
    assert _make({"mode": 1})[0].is_on is True, "Full Green → on"
    assert _make({"mode": 2})[0].is_on is True, "Eco Smart → on"
    assert _make(None)[0].is_on is None, "no eco data → unknown"
    # Garbage mode value → unknown, never a crash.
    assert _make({"mode": "x"})[0].is_on is None


# ── turn_on / turn_off write s_ecos ─────────────────────────────────
@case
def test_turn_on_writes_solar_mode_default_full_green():
    s, coord = _make({"mode": 0, "power_pct": 90})
    asyncio.run(s.async_turn_on())
    assert len(coord.client.calls) == 1, coord.client.calls
    met, par = coord.client.calls[0]
    payload = json.loads(par)
    assert met == "s_ecos"
    assert payload["esm"] == 1 and payload["ese"] == 1, "default to Full Green (1)"
    assert payload["esp"] == 90, "preserve the solar power target"
    assert coord.refreshes == 1, "must request a refresh after the write"


@case
def test_turn_off_writes_disabled():
    s, coord = _make({"mode": 2, "power_pct": 100})
    asyncio.run(s.async_turn_off())
    met, par = coord.client.calls[0]
    payload = json.loads(par)
    assert met == "s_ecos" and payload["esm"] == 0 and payload["ese"] == 0, payload


@case
def test_turn_on_restores_last_solar_flavour():
    # Observe Eco Smart (2) first (is_on records it), then Disabled, then turn on
    # → should restore Eco Smart (2), not the Full-Green default.
    s, coord = _make({"mode": 2})
    assert s.is_on is True                       # records _last_solar_mode = 2
    coord.data["eco_smart"] = {"mode": 0}        # now disabled
    assert s.is_on is False
    asyncio.run(s.async_turn_on())
    payload = json.loads(coord.client.calls[0][1])
    assert payload["esm"] == 2, f"should restore last solar flavour (2), got {payload}"


@case
def test_no_eco_support_is_noop():
    # Charger without Eco-Smart: no s_ecos write on either action.
    s, coord = _make(None)
    asyncio.run(s.async_turn_on())
    asyncio.run(s.async_turn_off())
    assert coord.client.calls == [], "must not write s_ecos to a charger lacking Eco-Smart"


def main():
    if not _HA_OK:
        return
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
