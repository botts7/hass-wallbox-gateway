"""Number platform for the Wallbox Gateway integration.

One number in v0.2:
  - max_current  (6 - 32 A, hits /api/command?action=current&value=N)
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MAX_CURRENT_A, MIN_CURRENT_A
from .coordinator import GatewayCoordinator
from .entity import GatewayEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GatewayCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MaxCurrent(coordinator)])


class MaxCurrent(GatewayEntity, NumberEntity):
    """Max charging current slider, mirrors the dashboard's setCurrent()."""

    entity_description = NumberEntityDescription(
        key="max_current",
        translation_key="max_current",
        name="Max current",
        device_class=NumberDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        native_min_value=MIN_CURRENT_A,
        native_max_value=MAX_CURRENT_A,
        native_step=1,
        mode=NumberMode.SLIDER,
    )

    def __init__(self, coordinator: GatewayCoordinator) -> None:
        super().__init__(coordinator, "max_current")

    @property
    def native_value(self) -> float | None:
        # The charger reports its currently-allowed max current under a
        # few keys depending on firmware. Prefer the realtime "cm" key
        # (current max amps) the dashboard reads, fall back to status.
        cm = self._charger_status().get("cm")
        if isinstance(cm, (int, float)):
            return float(cm)
        ic = self._realtime().get("ic")
        if isinstance(ic, (int, float)):
            return float(ic)
        return None

    async def async_set_native_value(self, value: float) -> None:
        amps = int(round(value))
        amps = max(MIN_CURRENT_A, min(MAX_CURRENT_A, amps))
        await self.coordinator.client.get(
            f"/api/command?action=current&value={amps}&wait=5000"
        )
        await self.coordinator.async_request_refresh()
