"""DataUpdateCoordinator for the Wallbox BLE Gateway.

One coordinator per config entry. Polls /api/status + /api/charger +
/api/diag/disconnects + /api/health in parallel each tick and shapes
the result into a single dict the entity platforms slice into.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GatewayAuthError, GatewayClient, GatewayUnreachable
from .const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    ENDPOINT_BOOT,
    ENDPOINT_CHARGE_LOG,
    ENDPOINT_CHARGER,
    ENDPOINT_DIAG,
    ENDPOINT_HEALTH,
    ENDPOINT_STATUS,
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
        interval = entry.options.get(
            CONF_POLL_INTERVAL,
            entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        )
        super().__init__(
            hass,
            LOGGER,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=timedelta(seconds=interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        # The endpoint reads below are pure HTTP and succeed/fail the coordinator
        # as a unit. The BAPI passthroughs (further down) each trigger a live BLE
        # round-trip on the gateway; they're best-effort (only work when BLE is
        # connected) and gathered with return_exceptions=True so a charger sleep
        # window falls back to the prior value instead of tripping every sensor.
        # They're also rate-shaped — live reads every cycle, config reads on a
        # slow cadence — so we don't saturate the gateway BLE pipeline (#168).
        try:
            status, charger, diag, health, boot, charge_log = await asyncio.gather(
                self.client.get(ENDPOINT_STATUS, timeout=4),
                self.client.get(ENDPOINT_CHARGER, timeout=4),
                self.client.get(ENDPOINT_DIAG, timeout=4),
                self.client.get(ENDPOINT_HEALTH, timeout=4),
                self.client.get(ENDPOINT_BOOT, timeout=4),
                self.client.get(ENDPOINT_CHARGE_LOG, timeout=4),
            )
        except GatewayAuthError as e:
            # Surface as auth-failed so HA starts the reauth flow (prompts
            # the user for new credentials) rather than just retrying.
            raise ConfigEntryAuthFailed(f"auth rejected by gateway: {e}") from e
        except GatewayUnreachable as e:
            raise UpdateFailed(f"gateway unreachable: {e}") from e

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
        self._poll_cycle = getattr(self, "_poll_cycle", 0) + 1
        if self._poll_cycle % _SLOW_POLL_EVERY == 1:
            (
                autolock_raw,
                ecos_raw,
                psh_raw,
                phsw_raw,
                tzn_raw,
                halo_raw,  # g_halocfg = LED halo config {bright %, mode, time_s}
            ) = await asyncio.gather(
                self.client.bapi("g_alo", wait_ms=2000),
                self.client.bapi("g_ecos", wait_ms=2000),
                self.client.bapi("g_psh", wait_ms=2000),
                self.client.bapi("g_phsw", wait_ms=2000),
                self.client.bapi("g_tzn", wait_ms=2000),
                self.client.bapi("g_halocfg", wait_ms=2000),
                return_exceptions=True,
            )
        else:
            autolock_raw = ecos_raw = psh_raw = phsw_raw = tzn_raw = halo_raw = None

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

        prior = self.data or {}
        return {
            "raw_status": status or {},
            # `status`/`realtime` can be the JSON literal null (empty cache on a
            # fresh boot or a marginal BLE link), so .get(x, {}) returns None,
            # not the default — guard with `or {}` before the nested .get (#20,
            # _Mike). Without this the whole coordinator crashes and every entity
            # goes unavailable.
            "charger_status": ((charger or {}).get("status") or {}).get("r", {}),
            "charger_realtime": ((charger or {}).get("realtime") or {}).get("r", {}),
            "diag": diag or {},
            "health": health or {},
            "boot": boot or {},
            "charge_log": (charge_log or {}).get("intervals", []) or prior.get("charge_log", []),
            "autolock": _parse_autolock(autolock_raw, prior.get("autolock")),
            "eco_smart": _parse_ecos(ecos_raw, prior.get("eco_smart")),
            "meter": _parse_dca(dca_raw, prior.get("meter")),
            "power_sharing": _parse_psh(psh_raw, prior.get("power_sharing")),
            "phase_switch": _parse_phsw(phsw_raw, prior.get("phase_switch")),
            "timezone": _parse_tzn(tzn_raw, prior.get("timezone")),
            "notifications": _parse_not(not_raw, prior.get("notifications")),
            "lse": _parse_lse(lse_raw, prior.get("lse")),
            "halo": _parse_halocfg(halo_raw, prior.get("halo")),
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
    """g_ecos returns {"r": {"esm": 0|1|2, "esp": 0-100, "ese": bool}}.
    esm 0 = Disabled, 1 = Full Green (solar-only), 2 = Eco Smart.
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
