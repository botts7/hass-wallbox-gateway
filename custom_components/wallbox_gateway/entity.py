"""Base entity for Wallbox Gateway entities.

All platform entities (sensor, binary_sensor, switch, ...) inherit
from this so they share the same DeviceInfo (one HA device per
gateway), the same coordinator wiring, and the same unique-id prefix
convention.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GatewayCoordinator


class GatewayEntity(CoordinatorEntity[GatewayCoordinator]):
    """Common base for every Wallbox Gateway entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GatewayCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        # Charger serial is the stable identifier; fall back to entry_id
        # for the rare case where /api/status hasn't returned it yet.
        sn = (coordinator.data.get("raw_status") or {}).get("chg_sn")
        self._device_serial = sn or coordinator.entry.entry_id
        self._attr_unique_id = f"{self._device_serial}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        status = self.coordinator.data.get("raw_status", {}) or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_serial)},
            name=self.coordinator.entry.title,
            manufacturer="Wallbox",
            model=status.get("chg_project") or "Pulsar",
            sw_version=status.get("chg_app_fw"),
            hw_version=status.get("dev_fw"),
            configuration_url=self.coordinator.client.base_url,
        )

    def _status(self) -> dict[str, Any]:
        return self.coordinator.data.get("raw_status", {}) or {}

    def _realtime(self) -> dict[str, Any]:
        return self.coordinator.data.get("charger_realtime", {}) or {}

    def _charger_status(self) -> dict[str, Any]:
        return self.coordinator.data.get("charger_status", {}) or {}

    def _is_zentri(self) -> bool:
        """True for the original (Zentri/TruConnect, pre-BGX) Pulsar (#12)."""
        return bool(self._status().get("zentri"))

    def _charger_status_code(self) -> int | None:
        """Live charger status code, charger-family aware.

        The original/Zentri Pulsar doesn't serve r_sta reliably, so its status
        lives in r_dat.st (the field the firmware itself uses for
        carConnected()). Everyone else uses r_sta.charger_status.
        """
        if self._is_zentri():
            code = self._charger_status().get("st")
            if code is None:
                code = self._realtime().get("charger_status")
        else:
            code = self._realtime().get("charger_status")
        try:
            return int(code) if code is not None else None
        except (TypeError, ValueError):
            return None

    def _diag(self) -> dict[str, Any]:
        return self.coordinator.data.get("diag", {}) or {}

    def _health(self) -> dict[str, Any]:
        return self.coordinator.data.get("health", {}) or {}

    def _boot(self) -> dict[str, Any]:
        return self.coordinator.data.get("boot", {}) or {}

    def _autolock(self) -> dict[str, Any]:
        return self.coordinator.data.get("autolock") or {}

    def _eco_smart(self) -> dict[str, Any]:
        return self.coordinator.data.get("eco_smart") or {}

    def _meter(self) -> dict[str, Any]:
        return self.coordinator.data.get("meter") or {}

    def _power_sharing(self) -> Any:
        return self.coordinator.data.get("power_sharing")

    def _phase_switch(self) -> Any:
        return self.coordinator.data.get("phase_switch")

    def _timezone(self) -> Any:
        return self.coordinator.data.get("timezone")

    def _notifications(self) -> dict[str, Any]:
        return self.coordinator.data.get("notifications") or {}

    def _lse(self) -> dict[str, Any]:
        return self.coordinator.data.get("lse") or {}

    def _halo(self) -> dict[str, Any]:
        """LED halo config: {"bright": %, "mode": 0/1 standby, "time_s": N}."""
        return self.coordinator.data.get("halo") or {}
