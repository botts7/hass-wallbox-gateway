"""DataUpdateCoordinator for the Wallbox BLE Gateway.

One coordinator per config entry. Polls /api/status + /api/charger +
/api/diag/disconnects + /api/health in parallel each tick and shapes
the result into a single dict the entity platforms slice into.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GatewayAuthError, GatewayClient
from .next_charge import compute_next_charge, plug_reminder_due
from .resilience import OnceSeen, cycles_for, due, first_exception, partition_results
from .const import (
    CHARGE_LOG_INTERVAL,
    CHARGE_LOG_TIMEOUT,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    ENDPOINT_BOOT,
    ENDPOINT_CHARGE_LOG,
    ENDPOINT_CHARGER,
    ENDPOINT_DIAG,
    ENDPOINT_HEALTH,
    ENDPOINT_STATUS,
    HTTP_TIMEOUT,
)

LOGGER = logging.getLogger(__name__)

# Minimum gateway firmware that emits the fields the entities read. Below this,
# older firmware can leave entities blank; we warn once so it's diagnosable.
MIN_GATEWAY_FW = "3.0.0"


def _fw_tuple(v: str) -> tuple[int, int, int]:
    """Parse 'v3.2.0-beta.7' / '3.0.0' / 'dev' to a comparable (maj, min, pat).
    A non-numeric build (dev/unknown) yields (0, 0, 0)."""
    parts = re.split(r"[.\-+]", (v or "").lstrip("v"))[:3]
    nums = [int(m.group()) if (m := re.match(r"\d+", p)) else 0 for p in parts]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


# Rarely-changing config BAPI reads (g_alo/g_ecos/g_psh/g_phsw/g_tzn/g_halocfg)
# are refreshed only every Nth poll cycle instead of every cycle. Each BAPI read
# is a live BLE round-trip on the gateway; a burst of ~9 concurrent reads every
# 10 s kept the gateway's BLE pipeline saturated (429 storm + task-watchdog risk,
# gateway #168). Live reads (r_dca/r_not/r_lse) stay on every cycle.
_SLOW_POLL_EVERY = 6

# Endpoints whose data the primary entities read. If one of these is gone the
# device really is unreachable and the update should fail. Everything else is
# secondary: it degrades to its previous value instead of taking the tick down
# with it (#8).
_CRITICAL_ENDPOINTS = ("status", "charger")


class GatewayCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the gateway, normalises responses, exposes one dict."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: GatewayClient,
    ) -> None:
        self.client = client
        self.entry = entry
        self._fw_warned = False
        self._poll_cycle = 0
        # One warning per (endpoint, exception type) and per unmapped status
        # code, instead of one per poll. See #8 and #9.
        self._endpoint_warned = OnceSeen()
        self.unknown_status_codes = OnceSeen()
        interval = entry.options.get(
            CONF_POLL_INTERVAL,
            entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        )
        # /api/charge_log rides its own wall-clock cadence rather than the tick.
        self._charge_log_every = cycles_for(CHARGE_LOG_INTERVAL, interval)
        super().__init__(
            hass,
            LOGGER,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=timedelta(seconds=interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        # The endpoint reads below are pure HTTP. /api/status and /api/charger
        # are critical — losing either fails the update. The rest degrade to
        # their prior value so one slow endpoint can't take the device down
        # (#8). The BAPI passthroughs (further down) each trigger a live BLE
        # round-trip on the gateway; they're best-effort (only work when BLE is
        # connected) and gathered with return_exceptions=True so a charger sleep
        # window falls back to the prior value instead of tripping every sensor.
        # They're also rate-shaped — live reads every cycle, config reads on a
        # slow cadence — so we don't saturate the gateway BLE pipeline (#168).
        prior = self.data or {}
        self._poll_cycle += 1

        # /api/charge_log is assembled over BLE on the gateway, so it is both the
        # slowest endpoint and the least time-critical one. Give it its own
        # ~5-minute cadence and a longer timeout rather than letting it ride the
        # 10 s tick on a 4 s budget (#8).
        names = ["status", "charger", "diag", "health", "boot"]
        calls = [
            self.client.get(ENDPOINT_STATUS, timeout=HTTP_TIMEOUT),
            self.client.get(ENDPOINT_CHARGER, timeout=HTTP_TIMEOUT),
            self.client.get(ENDPOINT_DIAG, timeout=HTTP_TIMEOUT),
            self.client.get(ENDPOINT_HEALTH, timeout=HTTP_TIMEOUT),
            self.client.get(ENDPOINT_BOOT, timeout=HTTP_TIMEOUT),
        ]
        if due(self._poll_cycle, self._charge_log_every):
            names.append("charge_log")
            calls.append(
                self.client.get(ENDPOINT_CHARGE_LOG, timeout=CHARGE_LOG_TIMEOUT)
            )

        # return_exceptions=True so one endpoint's failure doesn't cancel the
        # others' results. Before #8 a single charge_log timeout raised out of
        # the gather and every entity of the device went unavailable for the
        # cycle — including sensors whose own endpoint had answered fine.
        results = await asyncio.gather(*calls, return_exceptions=True)
        ok, failed = partition_results(names, results)

        # return_exceptions=True also captures CancelledError, which is not an
        # endpoint fault — it means HA is unloading the entry or shutting down.
        # Re-raise it rather than degrading, so teardown isn't swallowed.
        if (cancelled := first_exception(failed, asyncio.CancelledError)) is not None:
            raise cancelled

        # Auth is fatal wherever it appears: wrong credentials are wrong for
        # every endpoint. Surface as auth-failed so HA starts the reauth flow
        # (prompts the user for new credentials) rather than just retrying.
        if (auth_err := first_exception(failed, GatewayAuthError)) is not None:
            raise ConfigEntryAuthFailed(
                f"auth rejected by gateway: {auth_err}"
            ) from auth_err

        # Only /api/status and /api/charger carry the state the primary entities
        # read; losing either means the gateway really is unreachable.
        for name in _CRITICAL_ENDPOINTS:
            if (err := failed.get(name)) is not None:
                raise UpdateFailed(f"gateway unreachable: {err}") from err

        # Everything else degrades: keep the prior value, and say so once per
        # (endpoint, error type) rather than on every poll.
        for name, err in failed.items():
            if self._endpoint_warned.is_new((name, type(err).__name__)):
                LOGGER.warning(
                    "Wallbox gateway: %s could not be read (%s: %s). Keeping the "
                    "previous value; the rest of the device stays available. "
                    "Further identical failures on this endpoint are not logged.",
                    name,
                    type(err).__name__,
                    err,
                )
            else:
                LOGGER.debug("Wallbox gateway: %s failed again (%s)", name, err)

        status = ok.get("status")
        charger = ok.get("charger")
        diag = ok.get("diag")
        health = ok.get("health")
        boot = ok.get("boot")
        charge_log = ok.get("charge_log")

        # Live BAPI reads — every cycle. These change continuously while charging:
        #   r_dca = realtime power meter (per-phase voltage + power; not in /status)
        #   r_not = charger notifications/alerts
        #   r_lse = live session energy (solar/grid kWh split, surplus, control
        #           mode; user_id PII is dropped by _parse_lse — never an entity)
        dca_raw, not_raw, lse_raw = await asyncio.gather(
            self.client.bapi("r_dca", wait_ms=2000),
            self.client.bapi("r_not", wait_ms=2000),
            self.client.bapi("r_lse", wait_ms=2000),
            return_exceptions=True,
        )

        # Rarely-changing config reads — only every _SLOW_POLL_EVERY cycles (and
        # always on the first cycle so entities populate at startup). Skipped
        # cycles pass None → the _parse_* helpers return the prior value, so
        # entities never flap. Cuts the steady-state BAPI burst 9→3 per cycle.
        if due(self._poll_cycle, _SLOW_POLL_EVERY):
            (
                autolock_raw,
                ecos_raw,
                psh_raw,
                phsw_raw,
                tzn_raw,
                halo_raw,  # g_halocfg = LED halo config {bright %, mode, time_s}
                schs_raw,  # r_schs = native charge schedules (for next-charge calc)
            ) = await asyncio.gather(
                self.client.bapi("g_alo", wait_ms=2000),
                self.client.bapi("g_ecos", wait_ms=2000),
                self.client.bapi("g_psh", wait_ms=2000),
                self.client.bapi("g_phsw", wait_ms=2000),
                self.client.bapi("g_tzn", wait_ms=2000),
                self.client.bapi("g_halocfg", wait_ms=2000),
                self.client.bapi("r_schs", wait_ms=2000),
                return_exceptions=True,
            )
        else:
            autolock_raw = ecos_raw = psh_raw = phsw_raw = tzn_raw = halo_raw = schs_raw = None

        # Carry forward the prior settings dict when the BAPI read failed
        # (BLE napping, charger asleep, transient timeout) so the entities
        # don't flap to Unknown every time BLE blinks.
        # Warn once if the gateway firmware is older than what the entities need
        # (gw_fw added in firmware v3.2.0-beta.8). Closes the firmware <-> HA
        # compatibility axis: an old gateway can leave entities blank.
        gw_fw = (status or {}).get("gw_fw")
        if (
            gw_fw
            and not self._fw_warned
            and _fw_tuple(gw_fw)[0] > 0
            and _fw_tuple(gw_fw) < _fw_tuple(MIN_GATEWAY_FW)
        ):
            self._fw_warned = True
            LOGGER.warning(
                "Wallbox gateway firmware %s is older than %s — some entities may "
                "stay unavailable until you update the gateway firmware.",
                gw_fw,
                MIN_GATEWAY_FW,
            )

        # Charger-local next scheduled charge. The firmware's next_scheduled_charge
        # (in raw_status) is computed in UTC, so a local-midnight schedule lands a
        # day late; recompute it here with a real tz database from the native
        # schedules + the charger's zone. Recomputed every cycle (cheap) from the
        # carried-forward schedules so it advances as occurrences pass.
        timezone = _parse_tzn(tzn_raw, prior.get("timezone"))
        schedules = _parse_schedules(schs_raw, prior.get("schedules"))
        now_ts = time.time()
        next_local = compute_next_charge(schedules, timezone, now_ts)
        # Recompute the plug-in reminder against the tz-correct next charge — the
        # firmware's plug_reminder (in raw_status) uses its UTC next-charge, so
        # it's mistimed for local-midnight schedules. rem_lead + car_connected
        # come from /api/status; None means "fall back to the firmware flag".
        status_d = status or {}
        plug_local = plug_reminder_due(
            next_local, status_d.get("rem_lead"), status_d.get("car_connected"), now_ts)
        return {
            "raw_status": status or {},
            # `status`/`realtime` can be the JSON literal null (empty cache on a
            # fresh boot or a marginal BLE link), so .get(x, {}) returns None,
            # not the default — guard with `or {}` before the nested .get (#20,
            # _Mike). Without this the whole coordinator crashes and every entity
            # goes unavailable.
            "charger_status": ((charger or {}).get("status") or {}).get("r", {}),
            "charger_realtime": ((charger or {}).get("realtime") or {}).get("r", {}),
            # Secondary endpoints: a failed or skipped read carries the prior
            # value forward rather than blanking the entity (#8). `is None`
            # rather than `or` so a genuine empty response still replaces a
            # stale one.
            "diag": diag if diag is not None else prior.get("diag", {}),
            "health": health if health is not None else prior.get("health", {}),
            "boot": boot if boot is not None else prior.get("boot", {}),
            "charge_log": (charge_log or {}).get("intervals", []) or prior.get("charge_log", []),
            "autolock": _parse_autolock(autolock_raw, prior.get("autolock")),
            "eco_smart": _parse_ecos(ecos_raw, prior.get("eco_smart")),
            "meter": _parse_dca(dca_raw, prior.get("meter")),
            "power_sharing": _parse_psh(psh_raw, prior.get("power_sharing")),
            "phase_switch": _parse_phsw(phsw_raw, prior.get("phase_switch")),
            "timezone": timezone,
            "notifications": _parse_not(not_raw, prior.get("notifications")),
            "lse": _parse_lse(lse_raw, prior.get("lse")),
            "halo": _parse_halocfg(halo_raw, prior.get("halo")),
            "schedules": schedules,
            "next_scheduled_charge_local": next_local,
            "plug_reminder_local": plug_local,
        }


def _parse_autolock(raw: Any, prior: dict[str, Any] | None) -> dict[str, Any] | None:
    """g_alo returns {"r": N} (bare-int seconds) on Pulsar MAX or
    {"r": {"enabled": bool, "time": N}} on newer firmware. Normalise to
    {"enabled": bool, "seconds": int} so the switch + future number can
    read consistently.
    """
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if isinstance(r, dict):
        seconds = int(r.get("time") or 0)
        enabled = bool(r.get("enabled")) or seconds > 0
        return {"enabled": enabled, "seconds": seconds}
    if isinstance(r, (int, float)):
        seconds = int(r)
        return {"enabled": seconds > 0, "seconds": seconds}
    return prior


def _parse_ecos(raw: Any, prior: dict[str, Any] | None) -> dict[str, Any] | None:
    """g_ecos returns {"r": {"esm": 0|1, "esp": 0-100, "ese": bool}}.
    Documented Wallbox enum (#38): esm 0 = Eco (solar + grid), 1 = Full Green
    (solar-only); `ese` (active) is the on/off master. `mode` below is the raw
    esm — the select derives the user-facing Disabled/Full Green/Eco from
    (active, mode). There is no esm 2 (the charger silently ignores it).
    """
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if not isinstance(r, dict):
        return prior
    return {
        "mode": int(r.get("esm") or 0),
        "power_pct": int(r.get("esp") or 0),
        "active": bool(r.get("ese")),
    }


def _parse_halocfg(raw: Any, prior: dict[str, Any] | None) -> dict[str, Any] | None:
    """g_halocfg returns {"r": {"bright": 0-100, "mode": 0|1, "time_s": N}}.
    bright = LED brightness %, mode 1 = dim-when-idle (standby) on, time_s =
    standby dim timeout (s). Best-effort — carry the prior value on a failed
    read so the entities don't flap to Unknown when BLE blinks."""
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if not isinstance(r, dict) or "bright" not in r:
        return prior
    return {
        "bright": int(r.get("bright") or 0),
        "mode": int(r.get("mode") or 0),
        "time_s": int(r.get("time_s") or 0),
    }


def _parse_dca(raw: Any, prior: dict[str, Any] | None) -> dict[str, Any] | None:
    """r_dca returns {"r": {"v1"/"v2"/"v3": V, "p1"/"p2"/"p3": W,
    "c1"/"c2"/"c3": deci-A, ..., "e": Wh}} where v1..v3 are per-phase
    mains voltage (volts, direct), p1+p2+p3 sum to house power, c1..c3
    are per-phase house current in **deci-amps** (tenths of an amp — the
    firmware MQTT templates divide by 10, so we do the same), and e is
    the lifetime energy counter in Wh.
    """
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if not isinstance(r, dict):
        return prior

    def _volts(x: Any) -> int | None:
        return int(x) if isinstance(x, (int, float)) else None

    def _amps(x: Any) -> float | None:
        # c1..c3 are deci-amps; /10 → amps, matching the firmware
        # `(value_json.r.cN / 10) | round(1)` discovery templates.
        return round(int(x) / 10.0, 1) if isinstance(x, (int, float)) else None

    p1 = r.get("p1") or 0
    p2 = r.get("p2") or 0
    p3 = r.get("p3") or 0
    e = r.get("e")
    return {
        "voltage_v": _volts(r.get("v1")),
        # Per-phase mains voltage (EM340 / 3-phase Power Boost). Diagnostic.
        "voltage_l2_v": _volts(r.get("v2")),
        "voltage_l3_v": _volts(r.get("v3")),
        "house_power_w": int(p1) + int(p2) + int(p3),
        # Per-phase power (EM340 / 3-phase Power Boost). Diagnostic.
        "power_l1_w": int(p1),
        "power_l2_w": int(p2),
        "power_l3_w": int(p3),
        # Per-phase house current (deci-amps → amps).
        "house_current_a": _amps(r.get("c1")),
        "house_current_l2_a": _amps(r.get("c2")),
        "house_current_l3_a": _amps(r.get("c3")),
        # Lifetime energy counter — Wh from charger, exposed as kWh
        "lifetime_kwh": (int(e) / 1000.0) if isinstance(e, (int, float)) else None,
    }


def _parse_psh(raw: Any, prior: Any) -> bool | None:
    """g_psh returns {"r": {"dyps": bool}} on most firmware. Older
    builds returned a bare bool; we accept either shape.
    """
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if isinstance(r, dict):
        v = r.get("dyps")
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
    if isinstance(r, bool):
        return r
    return prior


def _parse_phsw(raw: Any, prior: Any) -> bool | None:
    """g_phsw returns {"r": {"enabled": bool}}. Some firmware returns
    a bare bool — accept either shape.
    """
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if isinstance(r, dict):
        v = r.get("enabled")
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
    if isinstance(r, bool):
        return r
    return prior


def _parse_tzn(raw: Any, prior: Any) -> str | None:
    """g_tzn returns {"r": {"timezone": "Europe/London"}}."""
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if isinstance(r, dict):
        tz = r.get("timezone")
        if isinstance(tz, str) and tz:
            return tz
    return prior


def _parse_schedules(raw: Any, prior: Any) -> list | None:
    """r_schs returns {"r": {"schedules": [...]}} (array models) or {"r": [...]}.
    Carries the prior list forward on a transient BLE miss so the derived
    next-charge doesn't flap to None every skipped/failed cycle."""
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if isinstance(r, dict) and isinstance(r.get("schedules"), list):
        return r["schedules"]
    if isinstance(r, list):
        return r
    return prior


def _parse_not(raw: Any, prior: dict[str, Any] | None) -> dict[str, Any] | None:
    """r_not returns {"r": [<notification objects>]} or {"r": 0} when
    there are none. We expose count + latest message text.
    """
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if isinstance(r, list):
        latest = ""
        if r:
            first = r[0]
            if isinstance(first, dict):
                latest = str(first.get("message") or first.get("msg") or first.get("text") or "")
            else:
                latest = str(first)
        return {"count": len(r), "latest": latest}
    if isinstance(r, (int, float)):
        return {"count": int(r), "latest": ""}
    return prior


def _parse_lse(raw: Any, prior: dict[str, Any] | None) -> dict[str, Any] | None:
    """r_lse is the live-session energy feed:
        {"r": {"green_energy": kWh, "grid_energy": kWh,
               "charged_energy": kWh, "charging_power": kW,
               "charging_time": s, "control_mode": int,
               "active_feature": {"feature": int, "feature_detail": int,
                                  "surplus_power": kW},
               "discharged_energy": kWh, "start_time": ts, "user_id": int}}

    We surface the solar/grid split, surplus power, active feature, and
    control mode. ``user_id`` is PII and is deliberately never read —
    it must not become an entity, an attribute, or a log line.
    """
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if not isinstance(r, dict):
        return prior
    af = r.get("active_feature")
    af = af if isinstance(af, dict) else {}

    def _num(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) else None

    return {
        "green_energy_kwh": _num(r.get("green_energy")),
        "grid_energy_kwh": _num(r.get("grid_energy")),
        "surplus_power_kw": _num(af.get("surplus_power")),
        "active_feature": int(af["feature"]) if isinstance(af.get("feature"), (int, float)) else None,
        "control_mode": int(r["control_mode"]) if isinstance(r.get("control_mode"), (int, float)) else None,
    }
