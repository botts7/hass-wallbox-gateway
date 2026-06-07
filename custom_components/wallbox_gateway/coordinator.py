"""DataUpdateCoordinator for the Wallbox BLE Gateway.

One coordinator per config entry. Polls /api/status + /api/charger +
/api/diag/disconnects + /api/health in parallel each tick and shapes
the result into a single dict the entity platforms slice into.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GatewayAuthError, GatewayClient, GatewayUnreachable
from .const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    ENDPOINT_CHARGER,
    ENDPOINT_DIAG,
    ENDPOINT_HEALTH,
    ENDPOINT_STATUS,
)

LOGGER = logging.getLogger(__name__)


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
        try:
            status, charger, diag, health = await asyncio.gather(
                self.client.get(ENDPOINT_STATUS, timeout=4),
                self.client.get(ENDPOINT_CHARGER, timeout=4),
                self.client.get(ENDPOINT_DIAG, timeout=4),
                self.client.get(ENDPOINT_HEALTH, timeout=4),
            )
        except GatewayAuthError as e:
            raise UpdateFailed(f"auth rejected by gateway: {e}") from e
        except GatewayUnreachable as e:
            raise UpdateFailed(f"gateway unreachable: {e}") from e

        # Unwrap the BAPI response envelopes -- /api/charger nests
        # status and realtime under `.r` keys; pre-flatten for entities.
        return {
            "raw_status": status or {},
            "charger_status": (charger or {}).get("status", {}).get("r", {}),
            "charger_realtime": (charger or {}).get("realtime", {}).get("r", {}),
            "diag": diag or {},
            "health": health or {},
        }
