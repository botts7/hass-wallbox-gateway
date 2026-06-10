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
    async_add_entities([
        RefreshNow(coordinator),
        ResumeSchedule(coordinator),
        RebootCharger(coordinator),
    ])


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


class ResumeSchedule(GatewayEntity, ButtonEntity):
    """Clears the manual-override flag (r_dat.gen -> 0) so the
    schedule + Eco Smart loops resume controlling the charger.
    Mirrors the Wallbox app's Resume button. Independent of
    charging state."""

    entity_description = ButtonEntityDescription(
        key="resume_schedule",
        translation_key="resume_schedule",
        name="Resume schedule",
        icon="mdi:play-circle",
    )

    def __init__(self, coordinator: GatewayCoordinator) -> None:
        super().__init__(coordinator, "resume_schedule")

    async def async_press(self) -> None:
        await self.coordinator.client.get("/api/command?action=resume")
        await self.coordinator.async_request_refresh()


class RebootCharger(GatewayEntity, ButtonEntity):
    """Reboot the charger itself (not the gateway). Mirrors the MQTT
    button.reboot — sends the BAPI `rebot` command via the gateway's
    /api/command shortcut. Diagnostic-category so it lives in the
    diagnostic section of the device page."""

    entity_description = ButtonEntityDescription(
        key="reboot_charger",
        translation_key="reboot_charger",
        name="Reboot charger",
        icon="mdi:restart",
        device_class=ButtonDeviceClass.RESTART,
    )

    def __init__(self, coordinator: GatewayCoordinator) -> None:
        super().__init__(coordinator, "reboot_charger")

    async def async_press(self) -> None:
        await self.coordinator.client.get("/api/command?action=reboot")
        await self.coordinator.async_request_refresh()
