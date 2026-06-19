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

# ---- Guided Charge Assistant (Options flow + native controller) ----
# Config lives under entry.options[CA_KEY] as a flat dict. See
# docs/design/guided-charge-assistant.md. Phase 1 = Reminder mode.
CA_KEY = "charge_assistant"
CA_MODE = "mode"
MODE_OFF = "off"
MODE_REMINDER = "reminder"
MODE_TARGET = "target_soc"    # Phase 2: charge to a target % then stop
MODE_SOLAR = "solar"          # Phase 3: charge from excess solar (surplus follow)
MODE_SCHEDULED = "scheduled"  # future phases
MODE_PROMPT = "prompt"        # future phases

# Target-SOC (smart charge) mode fields. Reuses CA_SOC_ENTITY (now
# required), CA_CHARGE_SWITCH (auto-resolved), CA_NOTIFY_SERVICE (optional).
CA_TARGET_PCT = "target_soc_pct"          # stop charging at/above this %
CA_TARGET_AUTOSTART = "target_autostart"  # also START when below target + plugged in

# Solar-surplus mode fields. Start when surplus >= start for `debounce`
# minutes; stop when surplus <= stop for `debounce` minutes (hysteresis +
# debounce ride out passing clouds). Thresholds are in the surplus sensor's
# own units (kW or W). Reuses CA_NOTIFY_SERVICE (optional).
CA_SURPLUS_ENTITY = "surplus_entity"
CA_SURPLUS_START = "surplus_start"          # start charging at/above this
CA_SURPLUS_STOP = "surplus_stop"            # stop charging at/below this
CA_SURPLUS_DEBOUNCE = "surplus_debounce_min"  # must hold this long before acting
# mobile_app notification action ids
CA_START_ACTION = "WB_CA_START"
CA_SNOOZE_ACTION = "WB_CA_SNOOZE"
CA_SKIP_ACTION = "WB_CA_SKIP"

# Field keys (shared across modes)
CA_REMINDER_ENTITY = "reminder_entity"
CA_NOTIFY_SERVICE = "notify_service"
CA_CHARGE_SWITCH = "charge_switch"
CA_SOC_ENTITY = "soc_entity"
CA_SKIP_ABOVE = "skip_above_pct"
CA_SOC_MAX_AGE = "soc_max_age_min"
CA_QUIET_START = "quiet_start"
CA_QUIET_END = "quiet_end"
CA_MESSAGE = "message"
CA_TITLE = "title"
CA_TAP_PATH = "tap_path"

# Reminder triggers (choose-your-own-adventure). CA_TRIGGERS holds the
# list of enabled trigger ids; each enabled trigger reads its own
# settings key below. All triggers share the conditions + notification.
CA_TRIGGERS = "triggers"
TRIG_ARRIVAL = "arrival"   # presence entity -> home
TRIG_NIGHTLY = "nightly"   # daily at a set time
TRIG_LEAD = "lead"         # N hours before the next scheduled charge
TRIG_TARIFF = "tariff"     # electricity price drops below a threshold

CA_ARRIVAL_ENTITY = "arrival_entity"      # person / device_tracker
CA_NIGHTLY_TIME = "nightly_time"          # "HH:MM:SS"
CA_LEAD_HOURS = "lead_hours"              # float hours before charge
CA_TARIFF_ENTITY = "tariff_entity"        # price sensor (e.g. Amber)
CA_TARIFF_BELOW = "tariff_below"          # notify when price <= this

# Conditions
CA_ONLY_IF_SCHEDULED = "only_if_scheduled"      # bool
CA_SCHEDULED_WITHIN_H = "scheduled_within_h"    # hours window for "scheduled"

# Notification behaviour
CA_ACTIONABLE = "actionable"        # bool — add Start/Snooze/Skip buttons
CA_ESCALATE_MIN = "escalate_min"    # re-remind after N min if still unplugged (0 = off)

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

# Eco-Smart mode integer -> HA select option key. Keys must match
# [a-z0-9-_]+ per HA's translation spec (hassfest fail otherwise).
# User-facing labels come from translations/en.json under
# entity.select.eco_smart_mode.state.{key}.
ECO_MODES = {
    0: "disabled",
    1: "full_green",
    2: "eco_smart",
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
