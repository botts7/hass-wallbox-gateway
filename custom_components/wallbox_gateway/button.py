"""Button platform for the Wallbox Gateway integration.

One button in v0.2:
  - refresh_now  (forces the coordinator to poll immediately)

reboot_gateway is intentionally deferred to v0.3 — POST /api/reboot on
the gateway requires a CSRF token paired with the browser session, and
the stateless integration can't obtain one without a session preflight.
A clean implementation needs a small firmware-side addition (auth-only
/api/v2/reboot or an integration-friendly token endpoint), which can't
land before 3.0's frozen firmware branch.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import GatewayCoordinator
from .entity import GatewayEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GatewayCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RefreshNow(coordinator)])


class RefreshNow(GatewayEntity, ButtonEntity):
    """Force-poll the gateway immediately, without waiting for the
    next coordinator tick. Useful after writing settings via curl or
    the dashboard when the user wants HA state to catch up now."""

    entity_description = ButtonEntityDescription(
        key="refresh_now",
        translation_key="refresh_now",
        name="Refresh now",
        device_class=ButtonDeviceClass.UPDATE,
    )

    def __init__(self, coordinator: GatewayCoordinator) -> None:
        super().__init__(coordinator, "refresh_now")

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()
