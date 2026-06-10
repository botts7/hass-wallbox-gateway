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
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent, UnitOfTime
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
    async_add_entities([
        MaxCurrent(coordinator),
        AutolockTime(coordinator),
        EcoSmartSolarTarget(coordinator),
    ])


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


class AutolockTime(GatewayEntity, NumberEntity):
    """Auto-lock timeout in minutes. Mirrors the MQTT autolock_time
    number entity. 0 disables; 1-60 sets the window."""

    entity_description = NumberEntityDescription(
        key="autolock_time",
        translation_key="autolock_time",
        name="Auto lock timeout",
        native_min_value=0,
        native_max_value=60,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
    )

    def __init__(self, coordinator: GatewayCoordinator) -> None:
        super().__init__(coordinator, "autolock_time")

    @property
    def native_value(self) -> float | None:
        seconds = self._autolock().get("seconds")
        if not isinstance(seconds, (int, float)):
            return None
        # Charger stores seconds; expose as minutes to match the
        # Wallbox app + MQTT entity.
        mins = (int(seconds) + 30) // 60
        return float(mins)

    async def async_set_native_value(self, value: float) -> None:
        mins = max(0, min(60, int(round(value))))
        seconds = mins * 60
        # s_alo write payload: bare-int seconds (Pulsar MAX shape).
        await self.coordinator.client.bapi(
            "s_alo", par=str(seconds), wait_ms=5000
        )
        await self.coordinator.async_request_refresh()


class EcoSmartSolarTarget(GatewayEntity, NumberEntity):
    """Eco Smart solar power target % (0-100). When in Full Green or
    Eco Smart mode, this controls how much of the available solar
    surplus the charger uses."""

    entity_description = NumberEntityDescription(
        key="eco_smart_solar_target",
        translation_key="eco_smart_solar_target",
        name="Eco Smart solar %",
        native_min_value=0,
        native_max_value=100,
        native_step=5,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
    )

    def __init__(self, coordinator: GatewayCoordinator) -> None:
        super().__init__(coordinator, "eco_smart_solar_target")

    @property
    def native_value(self) -> float | None:
        pct = self._eco_smart().get("power_pct")
        return float(pct) if isinstance(pct, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        pct = max(0, min(100, int(round(value))))
        # s_ecos shape: {esm: mode, ese: enabled, esp: pct}. Pull current
        # mode/enabled from coordinator so we only update the percentage.
        eco = self._eco_smart() or {}
        mode = int(eco.get("mode") or 0)
        active = bool(eco.get("active"))
        payload = (
            '{"esm":' + str(mode) +
            ',"ese":' + ("1" if active else "0") +
            ',"esp":' + str(pct) + "}"
        )
        await self.coordinator.client.bapi("s_ecos", par=payload, wait_ms=5000)
        await self.coordinator.async_request_refresh()
