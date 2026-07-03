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
        # The 4 endpoint reads are pure HTTP and always succeed/fail
        # the coordinator as a unit. The 2 BAPI reads (g_alo, g_ecos)
        # are best-effort: they only work when BLE is connected, and we
        # don't want a charger sleep window to mark the whole coordinator
        # as failed and trip every sensor's availability. So gather them
        # with return_exceptions=True and silently fall back to the
        # previously-cached value when they fail.
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

        (
            autolock_raw,
            ecos_raw,
            dca_raw,
            psh_raw,
            phsw_raw,
            tzn_raw,
            not_raw,
            lse_raw,
        ) = await asyncio.gather(
            self.client.bapi("g_alo", wait_ms=2000),
            self.client.bapi("g_ecos", wait_ms=2000),
            # r_dca = realtime power meter: per-phase voltage + power.
            # Required for the mains_voltage + house_power sensors.
            # /api/status doesn't include these — they live behind BAPI.
            self.client.bapi("r_dca", wait_ms=2000),
            # Additional settings for full MQTT-discovery parity (v0.3.0).
            # All best-effort with the same fallback semantics as the
            # original three: prior value carried forward on failure.
            self.client.bapi("g_psh", wait_ms=2000),
            self.client.bapi("g_phsw", wait_ms=2000),
            self.client.bapi("g_tzn", wait_ms=2000),
            self.client.bapi("r_not", wait_ms=2000),
            # r_lse = live session energy feed (v0.3.1): per-session
            # solar/grid kWh split, surplus power, active feature,
            # control mode. user_id in the response is PII and dropped
            # by _parse_lse — never exposed as an entity.
            self.client.bapi("r_lse", wait_ms=2000),
            return_exceptions=True,
        )

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


def _parse_dca(raw: Any, prior: dict[str, Any] | None) -> dict[str, Any] | None:
    """r_dca returns {"r": {"v1": V, "p1": W, "p2": W, "p3": W,
    "c1": A, ..., "e": Wh}} where v1 is L1 voltage, p1+p2+p3 sum to
    house power, c1 is per-phase current, and e is lifetime energy
    counter in Wh.
    """
    if isinstance(raw, Exception) or not isinstance(raw, dict):
        return prior
    r = raw.get("r")
    if not isinstance(r, dict):
        return prior
    v1 = r.get("v1")
    p1 = r.get("p1") or 0
    p2 = r.get("p2") or 0
    p3 = r.get("p3") or 0
    c1 = r.get("c1")
    e = r.get("e")
    return {
        "voltage_v": int(v1) if isinstance(v1, (int, float)) else None,
        "house_power_w": int(p1) + int(p2) + int(p3),
        # Per-phase power (EM340 / 3-phase Power Boost). Diagnostic.
        "power_l1_w": int(p1),
        "power_l2_w": int(p2),
        "power_l3_w": int(p3),
        "house_current_a": int(c1) if isinstance(c1, (int, float)) else None,
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
