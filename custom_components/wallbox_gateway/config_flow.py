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
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    ClientConfig,
    GatewayAuthError,
    GatewayClient,
    GatewayUnreachable,
)
from .const import (
    CA_CHARGE_SWITCH,
    CA_KEY,
    CA_MESSAGE,
    CA_MODE,
    CA_NOTIFY_SERVICE,
    CA_QUIET_END,
    CA_QUIET_START,
    CA_REMINDER_ENTITY,
    CA_SKIP_ABOVE,
    CA_SOC_ENTITY,
    CA_SOC_MAX_AGE,
    CA_TAP_PATH,
    CA_TITLE,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    MODE_OFF,
    MODE_REMINDER,
)

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

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Guided Charge Assistant setup lives in the options flow."""
        return WallboxGatewayOptionsFlow()

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


class WallboxGatewayOptionsFlow(config_entries.OptionsFlow):
    """Guided Charge Assistant wizard (Phase 1: Reminder mode).

    Multi-step + conditional: the Mode step routes to the matching
    settings step. The Integration runs the behaviour itself
    (charge_assistant.ChargeAssistant) — no automation is generated.
    """

    def _ca(self) -> dict:
        return dict(self.config_entry.options.get(CA_KEY) or {})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            mode = user_input[CA_MODE]
            if mode == MODE_REMINDER:
                return await self.async_step_reminder()
            # "Off" (and not-yet-built modes) — store mode only, clears the assistant.
            return self.async_create_entry(title="", data={CA_KEY: {CA_MODE: mode}})

        schema = vol.Schema(
            {
                vol.Required(CA_MODE, default=self._ca().get(CA_MODE, MODE_OFF)): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": MODE_OFF, "label": "Off"},
                            {
                                "value": MODE_REMINDER,
                                "label": "Reminder — notify me if a scheduled charge is coming up and the car isn't plugged in",
                            },
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_reminder(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="", data={CA_KEY: {CA_MODE: MODE_REMINDER, **user_input}}
            )

        cur = self._ca()
        notify_opts = [
            f"notify.{name}"
            for name in sorted(self.hass.services.async_services().get("notify", {}))
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CA_REMINDER_ENTITY,
                    default=cur.get(CA_REMINDER_ENTITY, vol.UNDEFINED),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
                vol.Required(
                    CA_NOTIFY_SERVICE, default=cur.get(CA_NOTIFY_SERVICE, vol.UNDEFINED)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=notify_opts,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CA_CHARGE_SWITCH, default=cur.get(CA_CHARGE_SWITCH, vol.UNDEFINED)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Optional(
                    CA_SOC_ENTITY, default=cur.get(CA_SOC_ENTITY, vol.UNDEFINED)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="battery"
                    )
                ),
                vol.Optional(CA_SKIP_ABOVE, default=cur.get(CA_SKIP_ABOVE, 80)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=100, unit_of_measurement="%",
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Optional(CA_SOC_MAX_AGE, default=cur.get(CA_SOC_MAX_AGE, 60)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=1440, unit_of_measurement="min",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CA_QUIET_START, default=cur.get(CA_QUIET_START, "00:00:00")
                ): selector.TimeSelector(),
                vol.Optional(
                    CA_QUIET_END, default=cur.get(CA_QUIET_END, "00:00:00")
                ): selector.TimeSelector(),
                vol.Optional(CA_TITLE, default=cur.get(CA_TITLE, "Wallbox")): str,
                vol.Optional(
                    CA_MESSAGE,
                    default=cur.get(
                        CA_MESSAGE,
                        "Your car isn't plugged in — a scheduled charge is coming up.",
                    ),
                ): str,
                vol.Optional(CA_TAP_PATH, default=cur.get(CA_TAP_PATH, "")): str,
            }
        )
        return self.async_show_form(step_id="reminder", data_schema=schema)
