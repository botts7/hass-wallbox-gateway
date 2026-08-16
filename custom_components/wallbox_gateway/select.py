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
        # Documented Wallbox eco_smart enum (#38): esm 0 = Eco (grid+solar),
        # 1 = Full Green. The master `active` (ese) flag is what turns it off —
        # esm alone can't distinguish Off from Eco. There is no esm 2.
        eco = self._eco_smart()
        if not eco:
            return None
        if not eco.get("active"):
            return ECO_MODES[0]  # "disabled"
        return ECO_MODES[1] if int(eco.get("mode", 0)) == 1 else ECO_MODES[2]

    async def async_select_option(self, option: str) -> None:
        if option not in ECO_MODE_TO_INT:
            return
        # Translate the HA option to the real {ese, esm} the charger expects
        # (esm 0 = Eco / "Solar + Grid", 1 = Full Green). "Disabled" drops the
        # master flag. Preserve esp so we don't reset the user's solar target.
        prior = self._eco_smart()
        esp = int(prior.get("power_pct") or 100)
        if option == ECO_MODES[0]:          # disabled
            payload = {"ese": 0, "esm": 0, "esp": esp}
        elif option == ECO_MODES[1]:        # full_green -> esm 1
            payload = {"ese": 1, "esm": 1, "esp": esp}
        else:                               # eco_smart (Solar + Grid) -> esm 0
            payload = {"ese": 1, "esm": 0, "esp": esp}
        await self.coordinator.client.bapi(
            "s_ecos", par=json.dumps(payload), wait_ms=8000
        )
        await self.coordinator.async_request_refresh()
