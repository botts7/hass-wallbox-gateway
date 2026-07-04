"""Firmware Update entity for the Wallbox BLE Gateway.

Exposes one HA Update entity that compares the running gateway firmware
(`gw_fw` from /api/status) against the latest GitHub release for the
esp32-wallbox firmware, and performs an OTA install by downloading the
matching release `.bin` and POSTing it to the gateway's `/api/ota`.

HA is the middleman: it fetches the release from GitHub and uploads it to
the gateway over the LAN — the gateway itself needs no internet. Manual
trigger only (HA's Install button); never auto-installs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .api import GatewayAuthError, GatewayError, GatewayUnreachable
from .const import (
    CONF_UPDATE_CHANNEL,
    DEFAULT_BOARD,
    DEFAULT_UPDATE_CHANNEL,
    DOMAIN,
    GITHUB_RELEASES_URL,
    UPDATE_CHANNEL_STABLE,
)
from .entity import GatewayEntity

_LOGGER = logging.getLogger(__name__)

# How often to re-check GitHub for a new release. Unauthenticated GitHub API is
# rate-limited to 60 requests/hour per IP; every few hours is well within budget.
_CHECK_INTERVAL = timedelta(hours=3)

# git-describe tail appended to non-release builds: "-<commits>-g<sha>[-dirty]"
# or a bare "-dirty" on a dirty checkout of a tagged commit. Stripped so a
# dev/dirty build of a release tag still compares equal to that release.
_GITDESC_TAIL = re.compile(r"-\d+-g[0-9a-f]+(?:-dirty)?$|-dirty$")


def normalize_version(v: str | None) -> str | None:
    """Reduce a firmware/tag string to a comparable version.

    Strips a leading 'v' and any git-describe suffix so
    'v3.2.0-beta.11-dirty' and tag 'v3.2.0-beta.11' compare equal.
    """
    if not v:
        return None
    v = v.strip().lstrip("vV")
    v = _GITDESC_TAIL.sub("", v)
    return v or None


def _version_key(tag: str | None) -> tuple:
    """Sortable semver key so beta.11 > beta.10 > beta.9 and a final release
    outranks its own pre-releases (3.2.0 > 3.2.0-beta.11).

    GitHub's /releases API is NOT ordered by version (observed order:
    beta.9, beta.11, beta.10, ...), so we must compare, not take the first.
    """
    v = normalize_version(tag) or "0"
    core, _, pre = v.partition("-")
    nums: list[int] = []
    for p in core.split("."):
        m = re.match(r"\d+", p)
        nums.append(int(m.group()) if m else 0)
    while len(nums) < 3:
        nums.append(0)
    if not pre:
        pre_key: tuple = (1,)  # a final release sorts above any pre-release
    else:
        # Tokenise "beta.11": numeric identifiers compare as ints, so
        # beta.11 > beta.9 (not the string order "11" < "9").
        toks = tuple(
            (1, int(part)) if part.isdigit() else (0, part)
            for part in pre.split(".")
        )
        pre_key = (0, toks)
    return (tuple(nums), pre_key)


def newest_release(releases: list[dict], channel: str) -> dict | None:
    """Highest-version release matching the channel.

    stable -> skip pre-releases and drafts; beta -> skip drafts only.
    Selected by semantic version (see _version_key), not list order.
    """
    best: dict | None = None
    best_key: tuple | None = None
    for rel in releases:
        if rel.get("draft"):
            continue
        if channel == UPDATE_CHANNEL_STABLE and rel.get("prerelease"):
            continue
        key = _version_key(rel.get("tag_name"))
        if best_key is None or key > best_key:
            best, best_key = rel, key
    return best


def pick_asset(release: dict, board: str) -> dict | None:
    """The firmware `.bin` asset for this board.

    Prefers an asset whose name ends with '-<board>.bin'; falls back to the
    only `.bin` when a release ships a single unambiguous target.
    """
    bins = [a for a in release.get("assets", []) if str(a.get("name", "")).endswith(".bin")]
    for a in bins:
        if str(a["name"]).endswith(f"-{board}.bin"):
            return a
    if len(bins) == 1:
        return bins[0]
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the firmware Update entity."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WallboxFirmwareUpdate(coordinator, entry)])


class WallboxFirmwareUpdate(GatewayEntity, UpdateEntity):
    """OTA-updates the ESP32 gateway firmware from GitHub releases."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES
    )
    _attr_translation_key = "firmware"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, "firmware_update")
        self._entry = entry
        self._latest_release: dict | None = None
        self._notes: str | None = None
        self._unsub = None
        self._attr_in_progress = False

    @property
    def _channel(self) -> str:
        return self._entry.options.get(CONF_UPDATE_CHANNEL, DEFAULT_UPDATE_CHANNEL)

    @property
    def _board(self) -> str:
        return self._status().get("board") or DEFAULT_BOARD

    @property
    def title(self) -> str:
        return "Wallbox Gateway Firmware"

    @property
    def installed_version(self) -> str | None:
        return normalize_version(self._status().get("gw_fw"))

    @property
    def latest_version(self) -> str | None:
        if not self._latest_release:
            # Unknown yet — report installed so HA shows "up to date", not a
            # spurious update, until the first GitHub check lands.
            return self.installed_version
        return normalize_version(self._latest_release.get("tag_name"))

    @property
    def release_url(self) -> str | None:
        return (self._latest_release or {}).get("html_url")

    async def async_release_notes(self) -> str | None:
        return self._notes

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_check()
        self._unsub = async_track_time_interval(
            self.hass, self._async_check_cb, _CHECK_INTERVAL
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()

    @callback
    def _async_check_cb(self, _now) -> None:
        self.hass.async_create_task(self._async_check())

    async def _async_check(self) -> None:
        """Refresh the latest-release info from GitHub (best-effort)."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                GITHUB_RELEASES_URL, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug("GitHub releases fetch: HTTP %s", resp.status)
                    return
                releases = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            _LOGGER.debug("GitHub releases fetch failed: %s", e)
            return
        rel = newest_release(releases or [], self._channel)
        self._latest_release = rel
        self._notes = ((rel or {}).get("body") or "").strip() or None
        self.async_write_ha_state()

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Download the release binary and OTA it to the gateway."""
        rel = self._latest_release
        if not rel:
            raise HomeAssistantError(
                "No release information available yet — try again in a moment."
            )
        asset = pick_asset(rel, self._board)
        if not asset:
            raise HomeAssistantError(
                f"No firmware asset for board '{self._board}' in "
                f"release {rel.get('tag_name')}."
            )
        url = asset.get("browser_download_url")
        session = async_get_clientsession(self.hass)
        # 1) Download the .bin from GitHub (HA is the middleman; gateway needs no net).
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status != 200:
                    raise HomeAssistantError(f"Firmware download failed: HTTP {resp.status}")
                data = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise HomeAssistantError(f"Firmware download failed: {e}") from e
        if not data:
            raise HomeAssistantError("Downloaded firmware image was empty.")
        md5 = hashlib.md5(data).hexdigest()
        _LOGGER.info(
            "OTA %s (%s, %d bytes) -> %s",
            rel.get("tag_name"), asset.get("name"), len(data), self._board,
        )
        # 2) Push to the gateway over the LAN. Mark in-progress so the HA card
        #    shows a spinner rather than blocking the Install action.
        self._attr_in_progress = True
        self.async_write_ha_state()
        try:
            await self.coordinator.client.ota(data, md5)
        except GatewayAuthError as e:
            self._attr_in_progress = False
            raise HomeAssistantError(f"Gateway rejected auth during OTA: {e}") from e
        except (GatewayError, GatewayUnreachable) as e:
            self._attr_in_progress = False
            raise HomeAssistantError(f"OTA upload failed: {e}") from e
        # 3) The gateway reboots into the new image. Do the settle-and-refresh
        #    OFF the Install service call so HA returns promptly (blocking here
        #    for the full reboot makes the frontend time out and show an error
        #    even though the flash succeeded). The background task clears
        #    in_progress once the new gw_fw is read back.
        self.hass.async_create_task(self._finish_install())

    async def _finish_install(self) -> None:
        """Wait out the reboot, then refresh so installed_version updates."""
        await asyncio.sleep(15)
        self._attr_in_progress = False
        try:
            await self.coordinator.async_request_refresh()
        finally:
            self.async_write_ha_state()
