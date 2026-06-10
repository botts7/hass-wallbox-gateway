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
ENDPOINT_BOOT = "/api/boot/history"

# Eco-Smart mode integer -> HA select option label. Mirrors the
# Wallbox app's wording so users see familiar terminology.
ECO_MODES = {
    0: "Disabled",
    1: "Full Green",
    2: "Eco Smart",
}
ECO_MODE_TO_INT = {v: k for k, v in ECO_MODES.items()}

# Max current limits supported by the BAPI passthrough (matches the
# dashboard slider). Real-world charger range is 6 – 32 A on a Pulsar.
MIN_CURRENT_A = 6
MAX_CURRENT_A = 32

# Default auto-lock window we write when the switch is toggled on but
# no specific minutes value has been configured. Mirrors the dashboard
# default of 60 s.
DEFAULT_AUTOLOCK_SECONDS = 60
