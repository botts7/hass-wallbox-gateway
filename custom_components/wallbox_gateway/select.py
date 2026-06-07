"""Select platform for the Wallbox Gateway integration.

One select in v0.2:
  - eco_smart_mode  (Disabled / Full Green / Eco Smart)

The s_ecos BAPI shape is the {ese, esm, esp} object the dashboard
writes — we preserve the prior esp (solar power target %) when toggling
modes so we don't accidentally reset the user's solar target. A
dedicated number entity for esp lands in v0.3.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ECO_MODE_TO_INT, ECO_MODES, DOMAIN
from .coordinator import GatewayCoordinator
from .entity import GatewayEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GatewayCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EcoSmartMode(coordinator)])


class EcoSmartMode(GatewayEntity, SelectEntity):
    """Eco-Smart mode select, drives s_ecos."""

    entity_description = SelectEntityDescription(
        key="eco_smart_mode",
        translation_key="eco_smart_mode",
        name="Eco Smart mode",
        options=list(ECO_MODES.values()),
    )
    _attr_options = list(ECO_MODES.values())

    def __init__(self, coordinator: GatewayCoordinator) -> None:
        super().__init__(coordinator, "eco_smart_mode")

    @property
    def current_option(self) -> str | None:
        eco = self._eco_smart()
        if not eco:
            return None
        mode = int(eco.get("mode", 0))
        return ECO_MODES.get(mode)

    async def async_select_option(self, option: str) -> None:
        mode = ECO_MODE_TO_INT.get(option)
        if mode is None:
            return
        # Preserve esp (solar power target) and derive ese (enabled flag)
        # from the mode, matching the dashboard's saveEco() shape.
        prior = self._eco_smart()
        payload = {
            "ese": 1 if mode > 0 else 0,
            "esm": mode,
            "esp": int(prior.get("power_pct") or 100),
        }
        await self.coordinator.client.bapi(
            "s_ecos", par=json.dumps(payload), wait_ms=8000
        )
        await self.coordinator.async_request_refresh()
