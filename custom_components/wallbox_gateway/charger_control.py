"""Charger control abstraction — the seam for supporting other chargers.

The Charge Assistant "brain" (charge_assistant.py) drives charging only through
a ``ChargerControl`` instance, never the Wallbox gateway directly. Today there
is one implementation, ``WallboxGatewayCharger``, which wraps the gateway's
owner-tagged ``/api/command`` and its cached status. Supporting a completely
different charger (Easee, Zappi, OpenEVSE, OCPP, or just an HA switch + number)
means writing another ``ChargerControl`` subclass with the same small contract
— no change to the modes / planner / GUI.

Kept dependency-light (no Home Assistant imports) so the command-building and
capability logic are unit-testable in isolation. The current bounds mirror the
firmware's hardware range (6–32 A on a Pulsar).
"""

from __future__ import annotations

from dataclasses import dataclass

# Hardware current range (mirrors const.MIN/MAX_CURRENT_A and the firmware).
MIN_CURRENT_A = 6
MAX_CURRENT_A = 32

# Owner tag recorded on every command so the gateway (and our own
# manual-override detection) knows the integration issued it.
OWNER = "integration"


@dataclass(frozen=True)
class ChargerCapabilities:
    """What a given charger can actually do — drives feature gating in the GUI
    and guards in the controller, so we never offer/attempt the impossible."""
    model: str
    can_start_stop: bool
    can_set_current: bool
    has_meter: bool
    min_current: int
    max_current: int


class ChargerControl:
    """Contract every charger adapter implements. The brain depends only on
    this surface."""

    # — commands (owner-tagged, may be async) —
    async def start(self) -> None: raise NotImplementedError
    async def stop(self) -> None: raise NotImplementedError
    async def set_current(self, amps: int) -> None: raise NotImplementedError

    # — capabilities + state (cheap, synchronous reads) —
    def capabilities(self) -> ChargerCapabilities: raise NotImplementedError
    def control_owner(self) -> str: raise NotImplementedError
    def last_command(self) -> tuple[str, int]: raise NotImplementedError
    def house_power_w(self) -> float | None: raise NotImplementedError


class WallboxGatewayCharger(ChargerControl):
    """Adapter over the ESP32 Wallbox gateway. Holds the integration's
    DataUpdateCoordinator: ``coordinator.client.command(payload)`` sends BAPI
    over BLE; ``coordinator.data`` carries cached status + meter."""

    def __init__(self, coordinator):
        self._coord = coordinator

    # ---- internals ----
    def _status(self) -> dict:
        if self._coord is None or not getattr(self._coord, "data", None):
            return {}
        return self._coord.data.get("raw_status") or {}

    def _build(self, action: str, value=None) -> dict:
        payload = {"action": action, "owner": OWNER, "wait": "5000"}
        if value is not None:
            payload["value"] = str(value)
        return payload

    async def _send(self, action: str, value=None) -> None:
        await self._coord.client.command(self._build(action, value))

    # ---- commands ----
    async def start(self) -> None:
        await self._send("start")

    async def stop(self) -> None:
        await self._send("stop")

    async def set_current(self, amps: int) -> None:
        lo, hi = MIN_CURRENT_A, MAX_CURRENT_A
        await self._send("current", value=int(max(lo, min(amps, hi))))

    # ---- capabilities + state ----
    def capabilities(self) -> ChargerCapabilities:
        st = self._status()
        is_zentri = bool(st.get("zentri"))
        model = str(st.get("chg_project") or st.get("dev_model") or "unknown")
        return ChargerCapabilities(
            model=model,
            can_start_stop=True,
            # Original/Zentri Pulsar can't do live current control over BLE.
            can_set_current=not is_zentri,
            has_meter=bool(st.get("meter")),
            min_current=MIN_CURRENT_A,
            max_current=MAX_CURRENT_A,
        )

    def control_owner(self) -> str:
        return str(self._status().get("control_owner") or "")

    def last_command(self) -> tuple[str, int]:
        st = self._status()
        by = str(st.get("last_command_by") or "")
        try:
            age = int(st.get("last_command_age_s"))
        except (TypeError, ValueError):
            age = -1
        return by, age

    def house_power_w(self) -> float | None:
        if self._coord is None or not getattr(self._coord, "data", None):
            return None
        meter = self._coord.data.get("meter") or {}
        v = meter.get("house_power_w")
        return float(v) if isinstance(v, (int, float)) else None
