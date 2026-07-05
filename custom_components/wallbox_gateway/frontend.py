"""Register the Wallbox Lovelace card bundle with the HA frontend.

Serves ``custom_components/wallbox_gateway/www/wallbox-cards.js`` at a stable
URL and adds it as an extra frontend module, so the ``custom:wallbox-*`` cards
are available in Lovelace with **no manual resource setup** — they appear in
the card picker once the integration is installed/updated.

Registration is done once per Home Assistant start (guarded via a flag in
``hass.data``) and the module URL is version-stamped so browsers pick up a new
bundle after an integration upgrade instead of serving a cached one.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# URL the bundle (and its folder) is served under, and the flag that keeps the
# one-time registration idempotent across config entries / reloads.
URL_BASE = "/wallbox_gateway_cards"
BUNDLE = "wallbox-cards.js"
_FLAG = "_frontend_registered"


async def async_register_frontend(hass: HomeAssistant, version: str) -> None:
    """Serve + register the card bundle. Safe to call more than once."""
    data = hass.data.setdefault(DOMAIN, {})
    if data.get(_FLAG):
        return
    # Claim the flag synchronously (before any await) so two config entries
    # setting up concurrently can't both pass the guard and register twice.
    data[_FLAG] = True

    www = hass.config.path(f"custom_components/{DOMAIN}/www")

    # Serve the www/ folder at URL_BASE (files at URL_BASE/<name>).
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, www, False)]
        )
    except (RuntimeError, ValueError) as err:
        # Already registered on a prior setup — fine, keep going.
        _LOGGER.debug("Wallbox cards static path already registered: %s", err)
    except Exception as err:  # noqa: BLE001 - never block entry setup on this
        _LOGGER.warning("Could not serve Wallbox cards bundle: %s", err)
        return

    # Add the module so Lovelace loads the custom:wallbox-* card definitions.
    try:
        from homeassistant.components.frontend import add_extra_js_url

        suffix = f"?v={version}" if version else ""
        add_extra_js_url(hass, f"{URL_BASE}/{BUNDLE}{suffix}")
    except Exception as err:  # noqa: BLE001 - frontend may be absent in tests
        _LOGGER.warning("Could not register Wallbox cards JS module: %s", err)
        return

    _LOGGER.info("Registered Wallbox Lovelace cards (v%s)", version or "?")
