"""Wallbox BLE Gateway integration.

Sets up the gateway HTTP client + DataUpdateCoordinator per config
entry and forwards to the platform modules. The actual entities live
in sensor.py / binary_sensor.py / switch.py / etc.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ClientConfig, GatewayClient
from .charge_assistant import ChargeAssistant
from .const import DEFAULT_USERNAME, DOMAIN
from .coordinator import GatewayCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wallbox Gateway from a config entry."""
    session = async_get_clientsession(hass)
    client = GatewayClient(
        session,
        ClientConfig(
            host=entry.data[CONF_HOST],
            username=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
            password=entry.data.get(CONF_PASSWORD, ""),
        ),
    )
    coordinator = GatewayCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Guided Charge Assistant — runs the configured behaviour itself (no
    # generated automation). No-op until the user runs the Options flow.
    assistant = ChargeAssistant(hass, entry)
    await assistant.async_start()
    hass.data[DOMAIN].setdefault("_assistants", {})[entry.entry_id] = assistant
    # Re-run setup (and thus re-read the assistant config) on options change.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when the Charge Assistant options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down a config entry."""
    assistant = hass.data.get(DOMAIN, {}).get("_assistants", {}).pop(entry.entry_id, None)
    if assistant is not None:
        await assistant.async_stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
