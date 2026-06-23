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
    CA_ACTIONABLE,
    CA_ARRIVAL_ENTITY,
    CA_ESCALATE_MIN,
    CA_KEY,
    CA_LEAD_HOURS,
    CA_MESSAGE,
    CA_MODE,
    CA_NIGHTLY_TIME,
    CA_NOTIFY_SERVICE,
    CA_ONLY_IF_SCHEDULED,
    CA_QUIET_END,
    CA_QUIET_START,
    CA_SCHEDULED_WITHIN_H,
    CA_SKIP_ABOVE,
    CA_SOC_ENTITY,
    CA_SOC_MAX_AGE,
    CA_TAP_PATH,
    CA_BATTERY_KWH,
    CA_CHARGE_POWER_KW,
    CA_CHEAPEST,
    CA_DEPARTURE,
    CA_GRID_ENTITY,
    CA_GRID_EXPORT_NEGATIVE,
    CA_LOAD_ENTITY,
    CA_LOAD_LIMIT_W,
    CA_LOAD_POWER_ENTITY,
    CA_MAX_CURRENT,
    CA_MIN_CURRENT,
    CA_PRICE_CAP,
    CA_PRICE_ENTITY,
    CA_SOLAR_DYNAMIC,
    CA_SOLAR_ENTITY,
    CA_SURPLUS_SOURCE,
    CA_SUPPLY_PHASES,
    CA_SUPPLY_VOLTAGE,
    CA_SURPLUS_DEBOUNCE,
    CA_SURPLUS_ENTITY,
    CA_SURPLUS_START,
    CA_SURPLUS_STOP,
    CA_TRIP_TARGET,
    CA_TRIP_UNTIL,
    CA_TARGET_AUTOSTART,
    CA_TARGET_PCT,
    CA_TARIFF_BELOW,
    CA_TARIFF_ENTITY,
    CA_TITLE,
    CA_TRIGGERS,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    MODE_OFF,
    MODE_REMINDER,
    MODE_SOLAR,
    MODE_TARGET,
    TRIG_ARRIVAL,
    TRIG_LEAD,
    TRIG_NIGHTLY,
    TRIG_TARIFF,
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


def _conn_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Connection form, optionally pre-filled for reconfigure.

    Password is deliberately never pre-filled (it would expose the stored
    secret in the form); left blank on reconfigure it keeps the stored one.
    """
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=d.get(CONF_HOST, vol.UNDEFINED)): str,
            vol.Optional(
                CONF_USERNAME, default=d.get(CONF_USERNAME, DEFAULT_USERNAME)
            ): str,
            vol.Optional(CONF_PASSWORD, default=""): str,
            vol.Optional(
                CONF_POLL_INTERVAL,
                default=d.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            ): vol.All(int, vol.Range(min=5, max=300)),
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Change host / credentials / poll interval without re-adding.

        Re-probes the gateway and refuses to point an existing entry at a
        different charger (unique-id mismatch).
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            # Blank password keeps the stored one.
            password = user_input.get(CONF_PASSWORD) or entry.data.get(
                CONF_PASSWORD, ""
            )
            username = user_input.get(CONF_USERNAME, DEFAULT_USERNAME)
            session = async_get_clientsession(self.hass)
            try:
                meta = await _probe(
                    session, user_input[CONF_HOST], username, password
                )
            except GatewayUnreachable:
                errors["base"] = "cannot_connect"
            except GatewayAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 — config-flow surface
                _LOGGER.exception("unexpected reconfigure probe failure")
                errors["base"] = "unknown"
            else:
                if (
                    meta.get("chg_sn")
                    and entry.unique_id
                    and meta["chg_sn"] != entry.unique_id
                ):
                    errors["base"] = "wrong_device"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data={
                            **entry.data,
                            CONF_HOST: user_input[CONF_HOST],
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                            CONF_POLL_INTERVAL: user_input.get(
                                CONF_POLL_INTERVAL,
                                entry.data.get(
                                    CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                                ),
                            ),
                        },
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_conn_schema(entry.data),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Triggered when the gateway starts rejecting our credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Collect new credentials and validate them against the gateway."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, DEFAULT_USERNAME)
            password = user_input.get(CONF_PASSWORD, "")
            session = async_get_clientsession(self.hass)
            try:
                await _probe(session, entry.data[CONF_HOST], username, password)
            except GatewayUnreachable:
                errors["base"] = "cannot_connect"
            except GatewayAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 — config-flow surface
                _LOGGER.exception("unexpected reauth probe failure")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_USERNAME,
                    default=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
                ): str,
                vol.Optional(CONF_PASSWORD, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"host": entry.data.get(CONF_HOST, "")},
        )


class WallboxGatewayOptionsFlow(config_entries.OptionsFlow):
    """Guided Charge Assistant wizard — choose-your-own-adventure.

    Reminder mode is a multi-step, conditional flow: pick triggers, then
    only the steps/fields those triggers need, then shared conditions, then
    the notification. The Integration runs the behaviour itself
    (charge_assistant.ChargeAssistant) — no automation is generated.
    """

    def _cur(self) -> dict:
        """The currently-saved Charge Assistant config (for defaults)."""
        return dict(self.config_entry.options.get(CA_KEY) or {})

    # ---- step 1: mode ----
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            mode = user_input[CA_MODE]
            if mode == MODE_REMINDER:
                # Start a fresh accumulator for this run-through.
                self._ca: dict[str, Any] = {CA_MODE: MODE_REMINDER}
                return await self.async_step_triggers()
            if mode == MODE_TARGET:
                self._ca = {CA_MODE: MODE_TARGET}
                return await self.async_step_target()
            if mode == MODE_SOLAR:
                self._ca = {CA_MODE: MODE_SOLAR}
                return await self.async_step_solar()
            # "Off" (and not-yet-built modes) — store mode only.
            # Merge into existing options (don't replace the whole dict) so the
            # native flow doesn't wipe poll_interval / tariff / other keys the
            # Add-on set. async_create_entry(data=…) replaces entry.options
            # wholesale, so we spread the current options in first.
            return self.async_create_entry(
                title="", data={**self.config_entry.options, CA_KEY: {CA_MODE: mode}}
            )

        schema = vol.Schema(
            {
                vol.Required(CA_MODE, default=self._cur().get(CA_MODE, MODE_OFF)): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": MODE_OFF, "label": "Off"},
                            {
                                "value": MODE_REMINDER,
                                "label": "Reminder — nudge me to plug the car in",
                            },
                            {
                                "value": MODE_TARGET,
                                "label": "Smart charge — charge to a target % then stop",
                            },
                            {
                                "value": MODE_SOLAR,
                                "label": "Solar charge — charge from excess solar",
                            },
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    # ---- Solar-surplus ----
    async def async_step_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._ca.update(user_input)
            return self.async_create_entry(
                title="", data={**self.config_entry.options, CA_KEY: self._ca}
            )

        cur = self._cur()
        notify_opts = [
            f"notify.{name}"
            for name in sorted(self.hass.services.async_services().get("notify", {}))
        ]
        schema = vol.Schema(
            {
                # Surplus source — direct sensor, or derive from grid / solar+load.
                vol.Optional(
                    CA_SURPLUS_SOURCE, default=cur.get(CA_SURPLUS_SOURCE, "entity")
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "entity", "label": "I have a surplus sensor"},
                            {"value": "grid", "label": "Compute from grid power"},
                            {"value": "solar_load", "label": "Compute from solar − house load"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CA_SURPLUS_ENTITY, default=cur.get(CA_SURPLUS_ENTITY, vol.UNDEFINED)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="power")
                ),
                vol.Optional(
                    CA_GRID_ENTITY, default=cur.get(CA_GRID_ENTITY, vol.UNDEFINED)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="power")
                ),
                vol.Optional(
                    CA_GRID_EXPORT_NEGATIVE, default=cur.get(CA_GRID_EXPORT_NEGATIVE, True)
                ): selector.BooleanSelector(),
                vol.Optional(
                    CA_SOLAR_ENTITY, default=cur.get(CA_SOLAR_ENTITY, vol.UNDEFINED)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="power")
                ),
                vol.Optional(
                    CA_LOAD_ENTITY, default=cur.get(CA_LOAD_ENTITY, vol.UNDEFINED)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="power")
                ),
                vol.Required(CA_SURPLUS_START, default=cur.get(CA_SURPLUS_START, 1.4)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=100000, step=0.1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CA_SURPLUS_STOP, default=cur.get(CA_SURPLUS_STOP, 0)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=100000, step=0.1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(CA_SURPLUS_DEBOUNCE, default=cur.get(CA_SURPLUS_DEBOUNCE, 3)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=30, unit_of_measurement="min",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                # Phase 2 — dynamic current follow + house-load limit (advanced;
                # the gateway clamps to 6-32 A and ignores live current on the
                # original Pulsar).
                vol.Optional(
                    CA_SOLAR_DYNAMIC, default=cur.get(CA_SOLAR_DYNAMIC, False)
                ): selector.BooleanSelector(),
                vol.Optional(CA_MIN_CURRENT, default=cur.get(CA_MIN_CURRENT, 6)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=6, max=32, unit_of_measurement="A",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CA_MAX_CURRENT, default=cur.get(CA_MAX_CURRENT, 32)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=6, max=32, unit_of_measurement="A",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CA_SUPPLY_VOLTAGE, default=cur.get(CA_SUPPLY_VOLTAGE, 230)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=100, max=500, unit_of_measurement="V",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CA_SUPPLY_PHASES, default=cur.get(CA_SUPPLY_PHASES, 1)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=3, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(CA_LOAD_LIMIT_W, default=cur.get(CA_LOAD_LIMIT_W, 0)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=30000, step=100, unit_of_measurement="W",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CA_LOAD_POWER_ENTITY, default=cur.get(CA_LOAD_POWER_ENTITY, vol.UNDEFINED)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="power")
                ),
                vol.Optional(
                    CA_NOTIFY_SERVICE, default=cur.get(CA_NOTIFY_SERVICE, vol.UNDEFINED)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=notify_opts,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="solar", data_schema=schema)

    # ---- Smart charge (target-SOC) ----
    async def async_step_target(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._ca.update(user_input)
            return self.async_create_entry(
                title="", data={**self.config_entry.options, CA_KEY: self._ca}
            )

        cur = self._cur()
        notify_opts = [
            f"notify.{name}"
            for name in sorted(self.hass.services.async_services().get("notify", {}))
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CA_SOC_ENTITY, default=cur.get(CA_SOC_ENTITY, vol.UNDEFINED)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="battery")
                ),
                vol.Required(CA_TARGET_PCT, default=cur.get(CA_TARGET_PCT, 80)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=100, unit_of_measurement="%",
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Optional(
                    CA_TARGET_AUTOSTART, default=cur.get(CA_TARGET_AUTOSTART, False)
                ): selector.BooleanSelector(),
                vol.Optional(
                    CA_DEPARTURE, default=cur.get(CA_DEPARTURE, vol.UNDEFINED)
                ): selector.TimeSelector(),
                vol.Optional(
                    CA_BATTERY_KWH, default=cur.get(CA_BATTERY_KWH, vol.UNDEFINED)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=200, step=0.1, unit_of_measurement="kWh",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CA_CHARGE_POWER_KW, default=cur.get(CA_CHARGE_POWER_KW, 7.4)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=50, step=0.1, unit_of_measurement="kW",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                # Phase 3 — cheapest-window (needs a forecast-capable price entity).
                vol.Optional(
                    CA_CHEAPEST, default=cur.get(CA_CHEAPEST, False)
                ): selector.BooleanSelector(),
                vol.Optional(
                    CA_PRICE_ENTITY, default=cur.get(CA_PRICE_ENTITY, vol.UNDEFINED)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                # Phase 4 — battery care (trip target until a deadline) + price cap.
                vol.Optional(
                    CA_TRIP_TARGET, default=cur.get(CA_TRIP_TARGET, vol.UNDEFINED)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=100, unit_of_measurement="%",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CA_TRIP_UNTIL, default=cur.get(CA_TRIP_UNTIL, vol.UNDEFINED)
                ): selector.DateTimeSelector(),
                vol.Optional(
                    CA_PRICE_CAP, default=cur.get(CA_PRICE_CAP, vol.UNDEFINED)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, step=0.01, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CA_NOTIFY_SERVICE, default=cur.get(CA_NOTIFY_SERVICE, vol.UNDEFINED)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=notify_opts,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="target", data_schema=schema)

    # ---- step 2: which triggers ----
    async def async_step_triggers(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._ca[CA_TRIGGERS] = user_input.get(CA_TRIGGERS, [])
            return await self.async_step_setup()

        default = self._cur().get(CA_TRIGGERS, [TRIG_ARRIVAL])
        schema = vol.Schema(
            {
                vol.Required(CA_TRIGGERS, default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": TRIG_ARRIVAL, "label": "When I get home (presence turns home)"},
                            {"value": TRIG_NIGHTLY, "label": "Each evening at a set time"},
                            {"value": TRIG_LEAD, "label": "Before a scheduled charge"},
                            {"value": TRIG_TARIFF, "label": "When electricity price drops (e.g. Amber)"},
                        ],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="triggers", data_schema=schema)

    # ---- step 3: per-trigger settings (only the chosen ones) ----
    async def async_step_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._ca.update(user_input)
            return await self.async_step_conditions()

        cur = self._cur()
        triggers = self._ca.get(CA_TRIGGERS, [])
        fields: dict[Any, Any] = {}
        if TRIG_ARRIVAL in triggers:
            fields[
                vol.Required(CA_ARRIVAL_ENTITY, default=cur.get(CA_ARRIVAL_ENTITY, vol.UNDEFINED))
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["person", "device_tracker"])
            )
        if TRIG_NIGHTLY in triggers:
            fields[
                vol.Required(CA_NIGHTLY_TIME, default=cur.get(CA_NIGHTLY_TIME, "20:00:00"))
            ] = selector.TimeSelector()
        if TRIG_LEAD in triggers:
            fields[
                vol.Required(CA_LEAD_HOURS, default=cur.get(CA_LEAD_HOURS, 2))
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=24, step=0.5, unit_of_measurement="h",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        if TRIG_TARIFF in triggers:
            fields[
                vol.Required(CA_TARIFF_ENTITY, default=cur.get(CA_TARIFF_ENTITY, vol.UNDEFINED))
            ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
            fields[
                vol.Required(CA_TARIFF_BELOW, default=cur.get(CA_TARIFF_BELOW, 0.15))
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=100, step=0.01, mode=selector.NumberSelectorMode.BOX
                )
            )

        if not fields:  # no per-trigger setup needed — skip ahead
            return await self.async_step_conditions()
        return self.async_show_form(step_id="setup", data_schema=vol.Schema(fields))

    # ---- step 4: shared conditions ----
    async def async_step_conditions(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._ca.update(user_input)
            return await self.async_step_notify()

        cur = self._cur()
        schema = vol.Schema(
            {
                vol.Optional(
                    CA_SOC_ENTITY, default=cur.get(CA_SOC_ENTITY, vol.UNDEFINED)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="battery")
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
                    CA_ONLY_IF_SCHEDULED, default=cur.get(CA_ONLY_IF_SCHEDULED, False)
                ): selector.BooleanSelector(),
                vol.Optional(
                    CA_SCHEDULED_WITHIN_H, default=cur.get(CA_SCHEDULED_WITHIN_H, 12)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=48, unit_of_measurement="h",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CA_QUIET_START, default=cur.get(CA_QUIET_START, "00:00:00")
                ): selector.TimeSelector(),
                vol.Optional(
                    CA_QUIET_END, default=cur.get(CA_QUIET_END, "00:00:00")
                ): selector.TimeSelector(),
            }
        )
        return self.async_show_form(step_id="conditions", data_schema=schema)

    # ---- step 5: notification ----
    async def async_step_notify(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._ca.update(user_input)
            return self.async_create_entry(
                title="", data={**self.config_entry.options, CA_KEY: self._ca}
            )

        cur = self._cur()
        notify_opts = [
            f"notify.{name}"
            for name in sorted(self.hass.services.async_services().get("notify", {}))
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CA_NOTIFY_SERVICE, default=cur.get(CA_NOTIFY_SERVICE, vol.UNDEFINED)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=notify_opts,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CA_TITLE, default=cur.get(CA_TITLE, "Wallbox")): str,
                vol.Optional(
                    CA_MESSAGE,
                    default=cur.get(
                        CA_MESSAGE,
                        "Your car isn't plugged in — plug it in to charge.",
                    ),
                ): str,
                vol.Optional(
                    CA_ACTIONABLE, default=cur.get(CA_ACTIONABLE, True)
                ): selector.BooleanSelector(),
                vol.Optional(CA_ESCALATE_MIN, default=cur.get(CA_ESCALATE_MIN, 0)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=120, unit_of_measurement="min",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CA_TAP_PATH, default=cur.get(CA_TAP_PATH, "")): str,
            }
        )
        return self.async_show_form(step_id="notify", data_schema=schema)
