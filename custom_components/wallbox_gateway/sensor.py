"""Sensor platform for the Wallbox Gateway integration.

Six entities for the MVP (v0.1.0):
  - charger_status      (enum, mapped from /api/charger -> realtime.charger_status)
  - charging_power_kw   (kW, from /api/charger -> status.cp)
  - session_energy_kwh  (kWh, from /api/charger -> status.en / 100)
  - house_power_w       (W, sum of p1+p2+p3 from BAPI r_dca cached in /api/status if available)
  - mains_voltage_v     (V, from BAPI r_dca)
  - ble_rssi            (dBm signal-strength, from /api/status -> rssi)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfInformation,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STATUS_CODES, ZENTRI_STATUS_CODES
from .coordinator import GatewayCoordinator
from .entity import GatewayEntity


@dataclass(frozen=True, kw_only=True)
class GatewaySensorEntityDescription(SensorEntityDescription):
    """Describes a sensor + a callable that pulls its value from the coordinator data."""

    value_fn: Callable[[GatewayEntity], Any]
    # #129: sensors sourced from the BAPI power meter (r_dca). When the
    # charger has no Power Boost / Power Meter accessory, /api/status reports
    # "meter": false and these go unavailable instead of showing 0 — mirrors
    # the dashboard hiding the voltage / house-power cells.
    requires_meter: bool = False


def _status_label(entity: GatewayEntity) -> str | None:
    code = entity._charger_status_code()
    if code is None:
        return None
    # Zentri uses a different enum; its labels are reused from STATUS_CODES so
    # the ENUM `options` list stays valid.
    table = ZENTRI_STATUS_CODES if entity._is_zentri() else STATUS_CODES
    return table.get(code, STATUS_CODES.get(code, f"Code {code}"))


def _charging_power(entity: GatewayEntity) -> float | None:
    cp = entity._charger_status().get("cp")
    return float(cp) if isinstance(cp, (int, float)) else None


def _session_energy(entity: GatewayEntity) -> float | None:
    en = entity._charger_status().get("en")
    # Gateway returns kWh*100 as an integer; divide for display.
    return (en / 100.0) if isinstance(en, (int, float)) else None


def _ble_rssi(entity: GatewayEntity) -> int | None:
    rssi = entity._status().get("rssi")
    return int(rssi) if isinstance(rssi, (int, float)) else None


def _mains_voltage(entity: GatewayEntity) -> int | None:
    # L1 voltage from the BAPI r_dca power-meter call — the coordinator
    # polls r_dca alongside the HTTP endpoints and stuffs the parsed
    # values into the meter dict. /api/status doesn't carry these.
    v = entity._meter().get("voltage_v")
    return int(v) if isinstance(v, (int, float)) else None


def _next_charge(entity: GatewayEntity) -> datetime | None:
    # Charge-reminder engine (#127): the gateway computes the UTC epoch of
    # the next enabled schedule. A timestamp sensor needs a tz-aware
    # datetime; 0/absent means no upcoming schedule (or NTP not synced).
    epoch = entity._status().get("next_scheduled_charge")
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _control_owner(entity: GatewayEntity) -> str | None:
    # Charge-control arbitration: who may autonomously drive charging.
    o = entity._status().get("control_owner")
    return str(o) if o else None


def _house_power(entity: GatewayEntity) -> int | None:
    # p1+p2+p3 summed in the coordinator's _parse_dca. Positive = the
    # house is importing from grid; negative = exporting (typically
    # solar overproduction).
    p = entity._meter().get("house_power_w")
    return int(p) if isinstance(p, (int, float)) else None


SENSORS: tuple[GatewaySensorEntityDescription, ...] = (
    GatewaySensorEntityDescription(
        key="charger_status",
        translation_key="charger_status",
        name="Charger status",
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(STATUS_CODES.values())),
        value_fn=_status_label,
    ),
    GatewaySensorEntityDescription(
        key="charging_power",
        translation_key="charging_power",
        name="Charging power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value_fn=_charging_power,
    ),
    GatewaySensorEntityDescription(
        key="session_energy",
        translation_key="session_energy",
        name="Session energy",
        device_class=SensorDeviceClass.ENERGY,
        # TOTAL_INCREASING: energy device_class can't be MEASUREMENT (HA
        # rejects it). The per-session value resets each session, which
        # total_increasing models correctly (HA detects the reset).
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_session_energy,
    ),
    GatewaySensorEntityDescription(
        key="house_power",
        translation_key="house_power",
        name="House power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=_house_power,
        requires_meter=True,
    ),
    GatewaySensorEntityDescription(
        key="mains_voltage",
        translation_key="mains_voltage",
        name="Mains voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=_mains_voltage,
        requires_meter=True,
    ),
    GatewaySensorEntityDescription(
        key="ble_rssi",
        translation_key="ble_rssi",
        name="BLE RSSI",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_registry_enabled_default=False,  # diagnostic — off by default
        value_fn=_ble_rssi,
    ),
    # ------------------------------------------------------------------
    # v0.3.0 parity additions (task #110). Most are diagnostic and
    # disabled by default; users enable per device if they want them.
    # ------------------------------------------------------------------
    # NOTE: the old grid_energy / green_energy sensors were removed — they
    # read charger-status gen/grid, but `gen` is the schedule-paused flag
    # (not energy) so they always read ~0, and they duplicated the names of
    # the r_lse-backed grid_energy_session / green_energy_session sensors
    # below. Use those for per-session solar/grid split.
    GatewaySensorEntityDescription(
        key="discharge_energy",
        translation_key="discharge_energy",
        name="Discharge energy (V2H)",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_float(e._charger_status().get("den"), divisor=100),
    ),
    GatewaySensorEntityDescription(
        key="lifetime_energy",
        translation_key="lifetime_energy",
        name="Lifetime energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda e: e._meter().get("lifetime_kwh"),
        requires_meter=True,
    ),
    GatewaySensorEntityDescription(
        key="house_current",
        translation_key="house_current",
        name="House current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._meter().get("house_current_a"),
        requires_meter=True,
    ),
    # Live charger status (numeric counterparts of the existing enum)
    GatewaySensorEntityDescription(
        key="max_available_current",
        translation_key="max_available_current",
        name="Max available current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._realtime().get("max_available_current")),
    ),
    GatewaySensorEntityDescription(
        key="max_charging_current",
        translation_key="max_charging_current",
        name="Max charging current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._realtime().get("max_charging_current")),
    ),
    # Charge-interval capture (#141): the gateway records each real charge
    # burst (cp>0). last_burst_wh = the most recent completed burst's energy;
    # charge_log_count = how many bursts are stored. Both from /api/status.
    GatewaySensorEntityDescription(
        key="last_burst_energy",
        translation_key="last_burst_energy",
        name="Last charge burst",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_float(e._status().get("last_burst_wh"), divisor=1000),
    ),
    GatewaySensorEntityDescription(
        key="charge_log_count",
        translation_key="charge_log_count",
        name="Recorded charge bursts",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._status().get("charge_log_count")),
    ),
    GatewaySensorEntityDescription(
        key="ocpp_status",
        translation_key="ocpp_status",
        name="OCPP status",
        entity_registry_enabled_default=False,
        value_fn=lambda e: _ocpp_label(e._realtime().get("ocpp_status")),
    ),
    # Notifications
    GatewaySensorEntityDescription(
        key="notification_count",
        translation_key="notification_count",
        name="Active notifications",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda e: e._notifications().get("count"),
    ),
    GatewaySensorEntityDescription(
        key="notification_latest",
        translation_key="notification_latest",
        name="Latest notification",
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._notifications().get("latest") or "None",
    ),
    # Charger identity (diagnostic)
    GatewaySensorEntityDescription(
        key="chg_app_fw",
        translation_key="chg_app_fw",
        name="Charger firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._status().get("chg_app_fw") or None,
    ),
    GatewaySensorEntityDescription(
        key="chg_project",
        translation_key="chg_project",
        name="Charger project",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._status().get("chg_project") or None,
    ),
    GatewaySensorEntityDescription(
        key="chg_sessions",
        translation_key="chg_sessions",
        name="Total charging sessions",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._status().get("chg_sessions")),
    ),
    GatewaySensorEntityDescription(
        key="chg_power_boost",
        translation_key="chg_power_boost",
        name="Power Boost limit",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._status().get("chg_power_boost")),
    ),
    GatewaySensorEntityDescription(
        key="chg_lock_state",
        translation_key="chg_lock_state",
        name="Lock state",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: "Locked" if e._status().get("chg_lock_state") == 1 else "Unlocked",
    ),
    GatewaySensorEntityDescription(
        key="chg_net_ssid",
        translation_key="chg_net_ssid",
        name="Charger WiFi SSID",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._status().get("chg_net_ssid") or None,
    ),
    GatewaySensorEntityDescription(
        key="chg_net_ip",
        translation_key="chg_net_ip",
        name="Charger IP",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._status().get("chg_net_ip") or None,
    ),
    GatewaySensorEntityDescription(
        key="chg_net_signal",
        translation_key="chg_net_signal",
        name="Charger WiFi signal",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._status().get("chg_net_signal")),
    ),
    GatewaySensorEntityDescription(
        key="chg_grounding",
        translation_key="chg_grounding",
        name="Charger grounding",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._status().get("chg_grounding") or None,
    ),
    # Gateway identity (diagnostic)
    GatewaySensorEntityDescription(
        key="gateway_ip",
        translation_key="gateway_ip",
        name="Gateway IP",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._status().get("ip") or None,
    ),
    GatewaySensorEntityDescription(
        key="gateway_fw",
        translation_key="gateway_fw",
        name="Gateway firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._boot().get("current_fw") or None,
    ),
    GatewaySensorEntityDescription(
        key="dev_name",
        translation_key="dev_name",
        name="Charger name",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._status().get("dev_name") or None,
    ),
    GatewaySensorEntityDescription(
        key="dev_mfg",
        translation_key="dev_mfg",
        name="Charger manufacturer",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._status().get("dev_mfg") or None,
    ),
    GatewaySensorEntityDescription(
        key="dev_model",
        translation_key="dev_model",
        name="BLE radio model",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._status().get("dev_model") or None,
    ),
    GatewaySensorEntityDescription(
        key="dev_fw",
        translation_key="dev_fw",
        name="BLE module FW",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._status().get("dev_fw") or None,
    ),
    GatewaySensorEntityDescription(
        key="timezone",
        translation_key="timezone",
        name="Timezone",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._timezone(),
    ),
    # Gateway runtime diagnostics (from /api/health)
    GatewaySensorEntityDescription(
        key="boot_reason",
        translation_key="boot_reason",
        name="Last boot reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e._boot().get("current") or None,
    ),
    GatewaySensorEntityDescription(
        key="max_reentry",
        translation_key="max_reentry",
        name="Reentry tripwire",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._health().get("max_reentry")),
    ),
    GatewaySensorEntityDescription(
        key="tokens",
        translation_key="tokens",
        name="Rate-limit tokens",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._health().get("tokens")),
    ),
    GatewaySensorEntityDescription(
        key="loop_max_ms",
        translation_key="loop_max_ms",
        name="Loop max ms",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._health().get("loop_max_ms")),
    ),
    GatewaySensorEntityDescription(
        key="heap_min_ever",
        translation_key="heap_min_ever",
        name="Heap min watermark",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._health().get("heap_min_ever")),
    ),
    GatewaySensorEntityDescription(
        key="heap_free",
        translation_key="heap_free",
        name="Heap free",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._health().get("heap_free")),
    ),
    GatewaySensorEntityDescription(
        key="gw_uptime",
        translation_key="gw_uptime",
        name="Gateway uptime",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._health().get("uptime")),
    ),
    GatewaySensorEntityDescription(
        key="wifi_rssi",
        translation_key="wifi_rssi",
        name="WiFi signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._status().get("wifi_rssi")),
    ),
    # ---- Live-session feed (r_lse), v0.3.1 ----------------------------
    # Per-session solar/grid energy split + live solar surplus. MEASUREMENT
    # (not TOTAL_INCREASING) because each value resets when a new session
    # starts. user_id from r_lse is dropped in the coordinator — never here.
    GatewaySensorEntityDescription(
        key="green_energy_session",
        translation_key="green_energy_session",
        name="Green energy (session)",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda e: e._lse().get("green_energy_kwh"),
    ),
    GatewaySensorEntityDescription(
        key="grid_energy_session",
        translation_key="grid_energy_session",
        name="Grid energy (session)",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda e: e._lse().get("grid_energy_kwh"),
    ),
    GatewaySensorEntityDescription(
        key="surplus_power",
        translation_key="surplus_power",
        name="Solar surplus power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value_fn=lambda e: e._lse().get("surplus_power_kw"),
    ),
    GatewaySensorEntityDescription(
        key="active_feature",
        translation_key="active_feature",
        name="Active feature",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._lse().get("active_feature")),
    ),
    GatewaySensorEntityDescription(
        key="control_mode",
        translation_key="control_mode",
        name="Control mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: _opt_int(e._lse().get("control_mode")),
    ),
    # ---- Charge reminder (#127) ----------------------------------------
    # Gateway-computed next enabled schedule start. device_class TIMESTAMP
    # so HA renders it as a tz-aware time and blueprints can do time math.
    GatewaySensorEntityDescription(
        key="next_scheduled_charge",
        translation_key="next_scheduled_charge",
        name="Next scheduled charge",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_next_charge,
    ),
    # ---- Charge-control owner (arbitration) ----------------------------
    # Who is allowed to autonomously drive charging (set on the gateway's
    # /config page). Diagnostic so the user can see why the Charge Assistant
    # is or isn't acting. See esp32-wallbox docs/control-owner.md.
    GatewaySensorEntityDescription(
        key="control_owner",
        translation_key="control_owner",
        name="Charge control owner",
        icon="mdi:account-key",
        device_class=SensorDeviceClass.ENUM,
        options=["wallbox_schedule", "integration", "addon", "none"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_control_owner,
    ),
)


def _opt_int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) else None


def _opt_float(v: Any, divisor: float = 1.0) -> float | None:
    if isinstance(v, (int, float)):
        return float(v) / divisor if divisor != 1.0 else float(v)
    return None


OCPP_STATUS_LABELS = {
    0: "Not available",
    1: "Not configured",
    2: "Connected",
    3: "Charging",
}


def _ocpp_label(code: Any) -> str | None:
    if not isinstance(code, (int, float)):
        return None
    return OCPP_STATUS_LABELS.get(int(code), f"Code {int(code)}")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GatewayCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GatewaySensor(coordinator, description) for description in SENSORS
    )


class GatewaySensor(GatewayEntity, SensorEntity):
    """Coordinator-backed sensor with a description-driven value_fn."""

    entity_description: GatewaySensorEntityDescription

    def __init__(
        self,
        coordinator: GatewayCoordinator,
        description: GatewaySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        # #129: hide meter-sourced sensors when the charger has no power meter.
        # `meter` is absent on older firmware -> treat as present (don't hide).
        if self.entity_description.requires_meter and self._status().get("meter") is False:
            return False
        return super().available

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self)
