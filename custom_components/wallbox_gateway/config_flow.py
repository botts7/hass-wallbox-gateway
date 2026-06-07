"""Config flow for the Wallbox BLE Gateway.

User pastes the gateway's local IP, optionally a username + password
(if web auth is enabled). We probe /api/health to confirm and pull
the firmware version + charger serial as the device identifier.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientSession

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    ClientConfig,
    GatewayAuthError,
    GatewayClient,
    GatewayUnreachable,
)
from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, DEFAULT_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def _probe(
    session: ClientSession, host: str, username: str, password: str
) -> dict[str, Any]:
    """Confirm the gateway answers + return a brief metadata bundle."""
    client = GatewayClient(
        session, ClientConfig(host=host, username=username, password=password)
    )
    health = await client.get("/api/health", timeout=5)
    status = await client.get("/api/status", timeout=5)
    return {
        "uptime": health.get("uptime"),
        "chg_sn": status.get("chg_sn"),
        "chg_app_fw": status.get("chg_app_fw"),
    }


SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Optional(CONF_PASSWORD, default=""): str,
        vol.Optional(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
            int, vol.Range(min=5, max=300)
        ),
    }
)


class WallboxGatewayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Single-step config flow: gather connection info + probe."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                meta = await _probe(
                    session,
                    user_input[CONF_HOST],
                    user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                    user_input.get(CONF_PASSWORD, ""),
                )
            except GatewayUnreachable:
                errors["base"] = "cannot_connect"
            except GatewayAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 — config-flow surface
                _LOGGER.exception("unexpected config-flow probe failure")
                errors["base"] = "unknown"
            else:
                # Use charger SN as the unique-id so re-runs from a
                # different IP don't create duplicates. Falls back to
                # host if SN is unavailable.
                unique = meta.get("chg_sn") or user_input[CONF_HOST]
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured(updates=user_input)
                title = (
                    f"Wallbox {meta['chg_sn']}"
                    if meta.get("chg_sn")
                    else f"Wallbox @ {user_input[CONF_HOST]}"
                )
                return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=SCHEMA,
            errors=errors,
        )
