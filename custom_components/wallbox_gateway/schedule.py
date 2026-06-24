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

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.util import dt as dt_util

from .api import GatewayClient, GatewayError, GatewayUnreachable
from .const import CA_IMPORTED_SCHEDULES, DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_SET = "set_schedule"
SERVICE_DELETE = "delete_schedule"
SERVICE_IMPORT = "import_native_schedules"

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


def _utc_int_to_local_hhmm(value: object) -> str | None:
    """UTC HHMM integer (e.g. 1400) -> local 'HH:MM' (reverse of the setter)."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    h, m = divmod(v, 100)
    utc_now = dt_util.utcnow()
    utc_dt = utc_now.replace(hour=h % 24, minute=m % 60, second=0, microsecond=0)
    local_dt = dt_util.as_local(utc_dt)
    return f"{local_dt.hour:02d}:{local_dt.minute:02d}"


def _days_from_array(arr: object) -> list[str]:
    """Charger day-set -> ['mon','wed']. Accepts either the Mon..Sun bit-array
    [1,0,1,...] we *write*, or the bitmask integer r_schs *reads back*
    (bit0=Mon … bit6=Sun; e.g. 127 = every day, 32 = Saturday)."""
    if isinstance(arr, bool):
        return []
    if isinstance(arr, int):
        return [_DAY_ORDER[i] for i in range(7) if arr & (1 << i)]
    if isinstance(arr, list):
        return [_DAY_ORDER[i] for i, on in enumerate(arr[:7]) if on]
    return []


def _decode_schedule(row: object) -> dict | None:
    """A raw r_schs row -> the friendly shape the set_schedule service accepts."""
    if not isinstance(row, dict):
        return None
    target = row.get("target") or {}
    kwh = 0.0
    if isinstance(target, dict) and target.get("type") == 1:
        try:
            kwh = round(int(target.get("value", 0)) / 1000, 3)
        except (TypeError, ValueError):
            kwh = 0.0
    return {
        "sid": row.get("sid"),
        "start": _utc_int_to_local_hhmm(row.get("start")),
        "stop": _utc_int_to_local_hhmm(row.get("stop")),
        "days": _days_from_array(row.get("days")),
        "max_current": row.get("mcr"),
        "enabled": bool(row.get("enabled")),
        "energy_target_kwh": kwh,
    }


def _resolve_coord_entry(hass: HomeAssistant, device_id: str | None):
    """(coordinator, entry_id) for the targeted device, or the sole entry."""
    store = hass.data.get(DOMAIN, {})
    coordinators = {k: v for k, v in store.items() if k != "_assistants"}
    if device_id:
        device = dr.async_get(hass).async_get(device_id)
        if device:
            for entry_id in device.config_entries:
                if entry_id in coordinators:
                    return coordinators[entry_id], entry_id
        raise HomeAssistantError("That device isn't a Wallbox Gateway charger.")
    if len(coordinators) == 1:
        entry_id, coord = next(iter(coordinators.items()))
        return coord, entry_id
    raise HomeAssistantError(
        "Multiple Wallbox Gateways configured — pass device_id to pick one."
    )


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


def _schedule_rows(res: object) -> list:
    """Pull the schedule list out of an r_schs reply.

    The charger nests it as {"r": {"schedules": [...]}}; older/Zentri shapes
    return {"r": [...]}. Accept either.
    """
    r = res.get("r") if isinstance(res, dict) else None
    if isinstance(r, dict):
        rows = r.get("schedules")
        return rows if isinstance(rows, list) else []
    return r if isinstance(r, list) else []


async def _next_sid(client: GatewayClient) -> int:
    """Next free schedule id = max(existing) + 1 (0 if none)."""
    try:
        res = await client.command(
            {"action": "bapi", "met": "r_schs", "par": "null", "wait": "5000"}
        )
    except (GatewayError, GatewayUnreachable):
        return 0
    rows = _schedule_rows(res)
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


_IMPORT_SCHEMA = vol.Schema({vol.Optional("device_id"): cv.string})


async def _handle_import(hass: HomeAssistant, call: ServiceCall) -> dict:
    """Read the charger's native schedules and mirror a copy into HA.

    Native schedules are *paused* (not deleted) while the integration owns
    charge control, so this preserves a visible, persisted snapshot in the
    config entry — nothing is lost and the user never has to re-create them.
    """
    coord, entry_id = _resolve_coord_entry(hass, call.data.get("device_id"))
    try:
        res = await coord.client.command(
            {"action": "bapi", "met": "r_schs", "par": "null", "wait": "6000"}
        )
    except (GatewayError, GatewayUnreachable) as e:
        raise HomeAssistantError(f"Couldn't reach the charger: {e}") from e
    _raise_on_error(res, "import_native_schedules")
    schedules = [s for s in (_decode_schedule(r) for r in _schedule_rows(res)) if s]
    snapshot = {"at": dt_util.utcnow().isoformat(), "schedules": schedules}
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is not None:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CA_IMPORTED_SCHEDULES: snapshot}
        )
    _LOGGER.info("Wallbox Gateway: imported %d native schedule(s)", len(schedules))
    return {"count": len(schedules), "schedules": schedules}


def async_setup_schedule_services(hass: HomeAssistant) -> None:
    """Register the schedule services once (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET):
        return

    async def _set(call: ServiceCall) -> None:
        await _handle_set(hass, call)

    async def _delete(call: ServiceCall) -> None:
        await _handle_delete(hass, call)

    async def _import(call: ServiceCall) -> dict:
        return await _handle_import(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_SET, _set, schema=_SET_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DELETE, _delete, schema=_DELETE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT, _import, schema=_IMPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
