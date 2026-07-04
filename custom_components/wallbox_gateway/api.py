"""HTTP client for the Wallbox BLE Gateway.

Thin async wrapper around the gateway's /api/* endpoints. Lives on
the integration side so the coordinator stays focused on orchestration
and the entities never touch HTTP directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


class GatewayAuthError(Exception):
    """Raised on 401/403 — credentials wrong or auth disabled mid-flight."""


class GatewayUnreachable(Exception):
    """Raised when the gateway TCP/HTTP layer can't be talked to."""


class GatewayError(Exception):
    """Raised for everything else (5xx, malformed JSON, etc.)."""


@dataclass(frozen=True)
class ClientConfig:
    host: str
    username: str
    password: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}"


class GatewayClient:
    """Async HTTP client for the Wallbox BLE Gateway.

    `session` is HA's shared aiohttp ClientSession so we benefit from
    connection pooling and HA's lifecycle management.
    """

    def __init__(self, session: aiohttp.ClientSession, config: ClientConfig) -> None:
        self._session = session
        self._config = config

    @property
    def base_url(self) -> str:
        return self._config.base_url

    def _auth(self) -> aiohttp.BasicAuth | None:
        if not self._config.password:
            return None
        return aiohttp.BasicAuth(self._config.username, self._config.password)

    async def get(self, path: str, timeout: float = 6.0) -> Any:
        url = self._config.base_url + path
        try:
            async with self._session.get(
                url,
                auth=self._auth(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status in (401, 403):
                    raise GatewayAuthError(f"{resp.status} on {path}")
                if resp.status >= 500:
                    raise GatewayError(f"{resp.status} on {path}")
                if resp.status >= 400:
                    raise GatewayError(f"{resp.status} on {path}")
                return await resp.json(content_type=None)
        except aiohttp.ClientConnectorError as e:
            raise GatewayUnreachable(str(e)) from e
        except TimeoutError as e:
            raise GatewayUnreachable(f"timeout on {path}") from e

    async def post(self, path: str, timeout: float = 6.0) -> Any:
        """POST with no body — for auth-only side-effect endpoints like
        /api/reboot_gateway (which is POST so a stray browser GET can't fire it)."""
        url = self._config.base_url + path
        try:
            async with self._session.post(
                url,
                auth=self._auth(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status in (401, 403):
                    raise GatewayAuthError(f"{resp.status} on {path}")
                if resp.status >= 400:
                    raise GatewayError(f"{resp.status} on {path}")
                return await resp.json(content_type=None)
        except aiohttp.ClientConnectorError as e:
            raise GatewayUnreachable(str(e)) from e
        except TimeoutError as e:
            raise GatewayUnreachable(f"timeout on {path}") from e

    async def ota(self, firmware: bytes, md5: str, timeout: float = 180.0) -> Any:
        """Upload a firmware image to the gateway's OTA endpoint.

        Multipart `firmware` field + an `X-Firmware-MD5` header (the gateway
        verifies the MD5 before flashing, then reboots into the new image and
        answers 200 first). Used by the Update entity to OTA a GitHub release
        binary over the LAN.
        """
        url = self._config.base_url + "/api/ota"
        form = aiohttp.FormData()
        form.add_field(
            "firmware", firmware,
            filename="firmware.bin",
            content_type="application/octet-stream",
        )
        try:
            async with self._session.post(
                url,
                data=form,
                auth=self._auth(),
                headers={"X-Firmware-MD5": md5},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status in (401, 403):
                    raise GatewayAuthError(f"{resp.status} on /api/ota")
                if resp.status >= 400:
                    body = await resp.text()
                    raise GatewayError(f"{resp.status} on /api/ota: {body[:200]}")
                return await resp.json(content_type=None)
        except aiohttp.ClientConnectorError as e:
            raise GatewayUnreachable(str(e)) from e
        except TimeoutError as e:
            raise GatewayUnreachable("timeout on /api/ota") from e

    async def bapi(self, met: str, par: str = "null", wait_ms: int = 5000) -> Any:
        """Invoke a BAPI method via /api/command. Returns the parsed JSON
        body — caller decides what to do with `r:` / `error:` fields.
        """
        path = f"/api/command?action=bapi&met={met}&par={par}&wait={wait_ms}"
        return await self.get(path, timeout=(wait_ms / 1000.0) + 3.0)

    async def command(
        self, params: dict[str, str], timeout: float = 9.0
    ) -> Any:
        """GET /api/command with properly-encoded query params.

        Used for BAPI writes whose `par` is raw JSON (e.g. s_sch / clr_sch):
        building the query as a dict lets aiohttp percent-encode it exactly
        once, the way the dashboard's encodeURIComponent does.
        """
        url = self._config.base_url + "/api/command"
        try:
            async with self._session.get(
                url,
                params=params,
                auth=self._auth(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status in (401, 403):
                    raise GatewayAuthError(f"{resp.status} on /api/command")
                if resp.status >= 400:
                    body = await resp.text()
                    raise GatewayError(f"{resp.status} on /api/command: {body[:200]}")
                return await resp.json(content_type=None)
        except aiohttp.ClientConnectorError as e:
            raise GatewayUnreachable(str(e)) from e
        except TimeoutError as e:
            raise GatewayUnreachable("timeout on /api/command") from e
