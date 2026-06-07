"""Constants for the Wallbox BLE Gateway integration."""

from __future__ import annotations

DOMAIN = "wallbox_gateway"

# Config-entry keys
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_POLL_INTERVAL = "poll_interval"

DEFAULT_USERNAME = "admin"
DEFAULT_POLL_INTERVAL = 10  # seconds

# BAPI status code -> human label. Mirrors STATUS_CODES from the BLE
# protocol; same numbering jagheterfredrik/wallbox-ble documents.
STATUS_CODES = {
    0: "Ready",
    1: "Charging",
    2: "Connected — waiting for car",
    3: "Connected — waiting for schedule",
    4: "Paused",
    5: "Schedule end",
    6: "Locked",
    7: "Error",
    8: "Connected — waiting for current allocation",
    9: "Power sharing not configured",
    10: "Queued by Power Boost",
    11: "Discharging",
    12: "Connected — waiting for admin auth (MID)",
    13: "MID safety margin exceeded",
    14: "OCPP unavailable",
    15: "OCPP charge finishing",
    16: "OCPP reserved",
    17: "Updating",
    18: "Queued by Eco-Smart",
}

# Endpoints the coordinator polls on every refresh tick.
ENDPOINT_STATUS = "/api/status"
ENDPOINT_CHARGER = "/api/charger"
ENDPOINT_DIAG = "/api/diag/disconnects"
ENDPOINT_HEALTH = "/api/health"
