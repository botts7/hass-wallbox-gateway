"""Wallbox BLE Gateway integration.

Sets up the gateway HTTP client + DataUpdateCoordinator per config
entry and forwards to the platform modules. The actual entities live
in sensor.py / binary_sensor.py / switch.py / etc.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ClientConfig, GatewayClient
from .ca_config import sanitize_options
from .charge_assistant import ChargeAssistant
from .const import (
    CA_KEY,
    DEFAULT_USERNAME,
    DOMAIN,
)
from .coordinator import GatewayCoordinator
from .schedule import async_setup_schedule_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.UPDATE,
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

    # One-time migration: re-enable sensors we used to ship disabled-by-default.
    # Flipping entity_registry_enabled_default to True does NOT re-enable an
    # entity that was already registered as disabled under an older version, so
    # users who updated kept a disabled entity (e.g. "Max charging current",
    # forum #75). We only clear an INTEGRATION disable — never a USER one — so a
    # deliberate manual disable is preserved. Idempotent (no-op once enabled).
    _reenable_default_disabled(hass, entry)

    # Serve + register the custom Lovelace cards (custom:wallbox-*), once per
    # HA start. Version-stamped from the manifest for cache-busting on upgrade.
    try:
        from homeassistant.loader import async_get_integration

        from .frontend import async_register_frontend

        integration = await async_get_integration(hass, DOMAIN)
        await async_register_frontend(hass, str(integration.version or ""))
    except Exception as err:  # noqa: BLE001 - cards are optional, never block setup
        _LOGGER.warning("Wallbox cards registration skipped: %s", err)

    # Guided Charge Assistant — runs the configured behaviour itself (no
    # generated automation). No-op until the user runs the Options flow.
    assistant = ChargeAssistant(hass, entry)
    await assistant.async_start()
    hass.data[DOMAIN].setdefault("_assistants", {})[entry.entry_id] = assistant
    # Re-run setup (and thus re-read the assistant config) on options change.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # One-time service: fire a test reminder notification on demand, so the
    # notify path + config can be confirmed without faking entity states.
    if not hass.services.has_service(DOMAIN, "test_reminder"):
        async def _async_test_reminder(call: ServiceCall) -> None:
            for a in hass.data.get(DOMAIN, {}).get("_assistants", {}).values():
                await a.async_test()
        hass.services.async_register(DOMAIN, "test_reminder", _async_test_reminder)

    # Charge-schedule create/update/delete services (idempotent).
    async_setup_schedule_services(hass)

    # Config bridge: lets the Add-on (or any caller) read + write this entry's
    # options — the Charge Assistant config and other tunables — so the rich
    # Add-on GUI can be the primary config surface. The native options flow
    # remains a fallback that writes the same entry.options.
    if not hass.services.has_service(DOMAIN, "get_config"):
        def _find_entry(host: str | None) -> ConfigEntry | None:
            entries = hass.config_entries.async_entries(DOMAIN)
            if host:
                for e in entries:
                    if e.data.get(CONF_HOST) == host:
                        return e
                return None
            return entries[0] if entries else None

        async def _async_get_config(call: ServiceCall) -> dict:
            entry = _find_entry(call.data.get("host"))
            if entry is None:
                return {"found": False, "options": {}}
            return {
                "found": True,
                "host": entry.data.get(CONF_HOST),
                "options": dict(entry.options),
            }

        async def _async_set_config(call: ServiceCall) -> None:
            entry = _find_entry(call.data.get("host"))
            if entry is None:
                raise HomeAssistantError("No matching Wallbox Gateway entry")
            incoming = call.data.get("options") or {}
            # The allow-list lives in ca_config.sanitize_options — a single,
            # unit-tested source of truth so the GUI, this service, and the tests
            # agree on exactly which keys survive. A stray or hostile call can't
            # inject arbitrary keys into entry.options (reloaded live) or clobber
            # unrelated settings. Credentials live in entry.data (not
            # entry.options), so they're never reachable here.
            clean, ignored = sanitize_options(incoming)
            for msg in ignored:
                _LOGGER.warning("set_config: %s", msg)
            if not clean:
                return
            # Sub-dicts the caller owns (the whole Charge Assistant config under
            # CA_KEY, the tariff) are replaced wholesale, exactly as the options
            # flow does. The add_update_listener (_async_options_updated) then
            # reloads the entry + restarts the assistant with the new config.
            new_opts = {**entry.options, **clean}
            hass.config_entries.async_update_entry(entry, options=new_opts)

        hass.services.async_register(
            DOMAIN, "get_config", _async_get_config,
            schema=vol.Schema({vol.Optional("host"): str}),
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN, "set_config", _async_set_config,
            schema=vol.Schema({
                vol.Required("options"): dict,
                vol.Optional("host"): str,
            }),
        )

    return True


# Entities that were previously entity_registry_enabled_default=False and are
# now enabled-by-default. If a user never touched them, an update leaves them
# disabled; re-enable those (INTEGRATION-disabled only).
_REENABLE_UNIQUE_ID_SUFFIXES = ("_max_charging_current",)


def _reenable_default_disabled(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear an integration-set disable on entities now enabled by default."""
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            ent.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            and ent.unique_id.endswith(_REENABLE_UNIQUE_ID_SUFFIXES)
        ):
            registry.async_update_entity(ent.entity_id, disabled_by=None)
            _LOGGER.info(
                "Re-enabled %s (was disabled by an older version's default)",
                ent.entity_id,
            )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when the Charge Assistant options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting a stale / orphaned gateway device from the UI.

    HA only shows a per-device "Delete" button for a config-entry-owned
    device when the integration implements this hook. Without it a leftover
    device can be disabled but not removed (reported on the HA forum).

    An orphan appears when a device was first created keyed by the config
    entry_id — the fallback GatewayEntity uses before the charger serial is
    known (see entity.py) — and then re-homed to a serial-keyed device once
    the serial arrived. We allow removing any device that is NOT the one the
    coordinator is currently reporting, and refuse the live device so the
    working one can't be deleted by mistake (HA would just recreate it).
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    live_ids: set[tuple[str, str]] = set()
    if coordinator is not None and coordinator.data:
        serial = (coordinator.data.get("raw_status") or {}).get("chg_sn")
        # The live device is serial-keyed once the serial is known, otherwise
        # it is still the entry_id-keyed fallback — protect whichever applies.
        live_ids.add((DOMAIN, serial) if serial else (DOMAIN, entry.entry_id))
    return not any(ident in live_ids for ident in device_entry.identifiers)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down a config entry."""
    assistant = hass.data.get(DOMAIN, {}).get("_assistants", {}).pop(entry.entry_id, None)
    if assistant is not None:
        await assistant.async_stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
