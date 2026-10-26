"""Charge-schedule management services.

Exposes wallbox_gateway.set_schedule (create/update) and
wallbox_gateway.delete_schedule. These send the charger's native BAPI
schedule commands through the gateway's /api/command passthrough — no
firmware change needed. The payload shapes mirror exactly what the gateway
dashboard sends (decoded + proven on the live charger):

  * create/update: ``s_sch`` with
        {"schedules":[{sid, start:<int HHMM UTC>, stop:<int HHMM UTC>,
                       days:[Sun..Sat bit-array], mcr, type, enabled,
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
from .current_limits import current_ceiling
from .legacy_schedule import (
    days_bitmask,
    is_no_dispatch,
    pick_legacy_sid,
    wsch_clear_par,
    wsch_par,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_SET = "set_schedule"
SERVICE_DELETE = "delete_schedule"
SERVICE_IMPORT = "import_native_schedules"

# SUNDAY-first order — the charger stores `days` Sunday-first (bit0=Sun .. bit6=Sat),
# verified against the official Wallbox app (Sunday = 1, Mon-Fri = 62) and matching
# the add-on's schedule editor. This list is indexed by the charger's BIT, so it
# MUST lead with Sunday. (It used to be Monday-first, which wrote/read every day one
# earlier — e.g. a Friday schedule landed on Thursday. See issue #11.)
_DAY_ORDER = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]

_SET_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Optional("sid"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Required("start"): cv.string,   # "HH:MM" local
        vol.Required("stop"): cv.string,    # "HH:MM" local
        vol.Required("days"): vol.All(cv.ensure_list, [vol.In(_DAY_ORDER)]),
        # Upper bound is the charger's own ceiling (40 A on US Pulsar Plus,
        # #39), checked at call time; 80 is only a sanity bound. Unset = the
        # charger's full ceiling.
        vol.Optional("max_current"): vol.All(
            vol.Coerce(int), vol.Range(min=6, max=80)
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
    """['sun','tue'] -> [1,0,1,0,0,0,0] (Sun..Sat). The charger reads this array
    Sunday-first, so the order must match _DAY_ORDER (bit0=Sun)."""
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
    """Charger day-set -> ['sun','tue']. Accepts either the Sun..Sat bit-array
    [1,0,1,...] we *write*, or the bitmask integer r_schs *reads back*
    (bit0=Sun … bit6=Sat; e.g. 127 = every day, 1 = Sunday, 62 = Mon-Fri)."""
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


async def _existing_sids(client: GatewayClient) -> list[int]:
    """Schedule ids currently on the charger ([] if unreadable)."""
    try:
        res = await client.command(
            {"action": "bapi", "met": "r_schs", "par": "null", "wait": "5000"}
        )
    except (GatewayError, GatewayUnreachable):
        return []
    rows = _schedule_rows(res)
    return [int(r["sid"]) for r in rows if isinstance(r, dict) and "sid" in r]


async def _next_sid(client: GatewayClient) -> int:
    """Next free schedule id = max(existing) + 1 (0 if none)."""
    sids = await _existing_sids(client)
    return (max(sids) + 1) if sids else 0


async def _bapi(client: GatewayClient, met: str, par: str) -> object:
    try:
        return await client.command(
            {"action": "bapi", "met": met, "par": par, "wait": "6000"}
        )
    except (GatewayError, GatewayUnreachable) as e:
        raise HomeAssistantError(f"Couldn't reach the charger: {e}") from e


async def _set_legacy(
    client: GatewayClient, requested: int | None, start: int, stop: int, days: list[str]
) -> None:
    """Pre-6.11 Pulsar Plus: write via the packed legacy w_sch (#13). No
    enabled / current / energy-target fields exist on this firmware."""
    try:
        sid = pick_legacy_sid(requested, await _existing_sids(client))
        par = wsch_par(sid, start, stop, days_bitmask(days))
    except ValueError as e:
        raise HomeAssistantError(f"set_schedule: {e}") from e
    res = await _bapi(client, "w_sch", par)
    _raise_on_error(res, "set_schedule (legacy w_sch)")
    _LOGGER.info("Wallbox Gateway: set_schedule #%s via legacy w_sch %s -> %s", sid, par, res)


async def _handle_set(hass: HomeAssistant, call: ServiceCall) -> None:
    data = call.data
    coord, _entry_id = _resolve_coord_entry(hass, data.get("device_id"))
    client = coord.client
    ceiling = current_ceiling(getattr(coord, "data", None))
    max_current = data.get("max_current")
    if max_current is None:
        max_current = ceiling
    elif int(max_current) > ceiling:
        raise HomeAssistantError(
            f"set_schedule: max_current {max_current} A is above this charger's "
            f"limit of {ceiling} A"
        )
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
        "mcr": int(max_current),
        "type": 0,
        "enabled": 1 if data["enabled"] else 0,
        "target": target,
        "repeat": 1,
    }
    par = json.dumps({"schedules": [entry]}, separators=(",", ":"))
    _LOGGER.debug("set_schedule sid=%s par=%s", sid, par)
    res = await _bapi(client, "s_sch", par)
    if is_no_dispatch(res):
        # Firmware without s_sch (pre-6.11 Pulsar Plus): fall back (#13).
        await _set_legacy(
            client, data.get("sid"), entry["start"], entry["stop"], data["days"]
        )
        return
    _raise_on_error(res, "set_schedule")
    _LOGGER.info("Wallbox Gateway: set_schedule #%s -> %s", sid, res)


async def _handle_delete(hass: HomeAssistant, call: ServiceCall) -> None:
    data = call.data
    client = _resolve_client(hass, data.get("device_id"))
    sids = [int(s) for s in data["sid"]]
    par = json.dumps({"sid": sids}, separators=(",", ":"))
    _LOGGER.debug("delete_schedule par=%s", par)
    res = await _bapi(client, "clr_sch", par)
    if is_no_dispatch(res):
        # Firmware without clr_sch (pre-6.11 Pulsar Plus): empty each slot via
        # the legacy w_sch, as the gateway dashboard does (#13).
        for sid in sids:
            try:
                clear = wsch_clear_par(sid)
            except ValueError as e:
                raise HomeAssistantError(f"delete_schedule: {e}") from e
            r = await _bapi(client, "w_sch", clear)
            _raise_on_error(r, f"delete_schedule #{sid} (legacy w_sch)")
        _LOGGER.info("Wallbox Gateway: delete_schedule %s via legacy w_sch", sids)
        return
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
