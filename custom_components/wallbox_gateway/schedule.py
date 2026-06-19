"""Charge-schedule management services.

Exposes wallbox_gateway.set_schedule (create/update) and
wallbox_gateway.delete_schedule. These send the charger's native BAPI
schedule commands through the gateway's /api/command passthrough — no
firmware change needed. The payload shapes mirror exactly what the gateway
dashboard sends (decoded + proven on the live charger):

  * create/update: ``s_sch`` with
        {"schedules":[{sid, start:<int HHMM UTC>, stop:<int HHMM UTC>,
                       days:[Mon..Sun bit-array], mcr, type, enabled,
                       target, repeat}]}
    One method does both insert and update, keyed by ``sid``.
  * delete: ``clr_sch`` with {"sid":[<id>, ...]} (the key holds an ARRAY).

Times are entered as local HH:MM and converted to the UTC HHMM integers the
charger stores (matching the dashboard's local<->UTC handling).
"""

from __future__ import annotations

import json
import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.util import dt as dt_util

from .api import GatewayClient, GatewayError, GatewayUnreachable
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_SET = "set_schedule"
SERVICE_DELETE = "delete_schedule"

# Mon-first order — matches the dashboard's day bit-array (bit0=Mon..bit6=Sun).
_DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_SET_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Optional("sid"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Required("start"): cv.string,   # "HH:MM" local
        vol.Required("stop"): cv.string,    # "HH:MM" local
        vol.Required("days"): vol.All(cv.ensure_list, [vol.In(_DAY_ORDER)]),
        vol.Optional("max_current", default=32): vol.All(
            vol.Coerce(int), vol.Range(min=6, max=32)
        ),
        vol.Optional("enabled", default=True): cv.boolean,
        vol.Optional("energy_target_kwh", default=0): vol.All(
            vol.Coerce(float), vol.Range(min=0)
        ),
    }
)

_DELETE_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Required("sid"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
    }
)


def _local_hhmm_to_utc_int(hass: HomeAssistant, value: str) -> int:
    """'HH:MM'(:SS) local -> UTC HHMM integer (e.g. local 00:00 -> 1400)."""
    parts = str(value).split(":")
    h, m = int(parts[0]), int(parts[1])
    local_now = dt_util.now()
    local_dt = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
    utc_dt = dt_util.as_utc(local_dt)
    return utc_dt.hour * 100 + utc_dt.minute


def _days_array(days: list[str]) -> list[int]:
    """['mon','wed'] -> [1,0,1,0,0,0,0] (Mon..Sun)."""
    chosen = {d.lower() for d in days}
    return [1 if d in chosen else 0 for d in _DAY_ORDER]


def _resolve_client(hass: HomeAssistant, device_id: str | None) -> GatewayClient:
    """Find the GatewayClient for the targeted device, or the sole entry."""
    store = hass.data.get(DOMAIN, {})
    # Coordinators are stored under their entry_id (plus the "_assistants" key).
    coordinators = {k: v for k, v in store.items() if k != "_assistants"}
    if device_id:
        device = dr.async_get(hass).async_get(device_id)
        if device:
            for entry_id in device.config_entries:
                if entry_id in coordinators:
                    return coordinators[entry_id].client
        raise HomeAssistantError("That device isn't a Wallbox Gateway charger.")
    if len(coordinators) == 1:
        return next(iter(coordinators.values())).client
    raise HomeAssistantError(
        "Multiple Wallbox Gateways configured — pass device_id to pick one."
    )


def _raise_on_error(result: object, what: str) -> None:
    if isinstance(result, dict) and result.get("error"):
        err = result["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise HomeAssistantError(f"{what} rejected by charger: {msg}")


async def _next_sid(client: GatewayClient) -> int:
    """Next free schedule id = max(existing) + 1 (0 if none)."""
    try:
        res = await client.command(
            {"action": "bapi", "met": "r_schs", "par": "null", "wait": "5000"}
        )
    except (GatewayError, GatewayUnreachable):
        return 0
    rows = res.get("r") if isinstance(res, dict) else None
    if not isinstance(rows, list) or not rows:
        return 0
    sids = [int(r["sid"]) for r in rows if isinstance(r, dict) and "sid" in r]
    return (max(sids) + 1) if sids else 0


async def _handle_set(hass: HomeAssistant, call: ServiceCall) -> None:
    data = call.data
    client = _resolve_client(hass, data.get("device_id"))
    sid = data.get("sid")
    if sid is None:
        sid = await _next_sid(client)
    kwh = float(data["energy_target_kwh"])
    target = {"type": 1, "value": int(kwh * 1000)} if kwh > 0 else {"type": 0, "value": 0}
    entry = {
        "sid": int(sid),
        "start": _local_hhmm_to_utc_int(hass, data["start"]),
        "stop": _local_hhmm_to_utc_int(hass, data["stop"]),
        "days": _days_array(data["days"]),
        "mcr": int(data["max_current"]),
        "type": 0,
        "enabled": 1 if data["enabled"] else 0,
        "target": target,
        "repeat": 1,
    }
    par = json.dumps({"schedules": [entry]}, separators=(",", ":"))
    _LOGGER.debug("set_schedule sid=%s par=%s", sid, par)
    try:
        res = await client.command(
            {"action": "bapi", "met": "s_sch", "par": par, "wait": "6000"}
        )
    except (GatewayError, GatewayUnreachable) as e:
        raise HomeAssistantError(f"Couldn't reach the charger: {e}") from e
    _raise_on_error(res, "set_schedule")
    _LOGGER.info("Wallbox Gateway: set_schedule #%s -> %s", sid, res)


async def _handle_delete(hass: HomeAssistant, call: ServiceCall) -> None:
    data = call.data
    client = _resolve_client(hass, data.get("device_id"))
    sids = [int(s) for s in data["sid"]]
    par = json.dumps({"sid": sids}, separators=(",", ":"))
    _LOGGER.debug("delete_schedule par=%s", par)
    try:
        res = await client.command(
            {"action": "bapi", "met": "clr_sch", "par": par, "wait": "6000"}
        )
    except (GatewayError, GatewayUnreachable) as e:
        raise HomeAssistantError(f"Couldn't reach the charger: {e}") from e
    _raise_on_error(res, "delete_schedule")
    _LOGGER.info("Wallbox Gateway: delete_schedule %s -> %s", sids, res)


def async_setup_schedule_services(hass: HomeAssistant) -> None:
    """Register the schedule services once (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET):
        return

    async def _set(call: ServiceCall) -> None:
        await _handle_set(hass, call)

    async def _delete(call: ServiceCall) -> None:
        await _handle_delete(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_SET, _set, schema=_SET_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DELETE, _delete, schema=_DELETE_SCHEMA)
