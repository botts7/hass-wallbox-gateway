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
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STATUS_CODES
from .coordinator import GatewayCoordinator
from .entity import GatewayEntity


@dataclass(frozen=True, kw_only=True)
class GatewaySensorEntityDescription(SensorEntityDescription):
    """Describes a sensor + a callable that pulls its value from the coordinator data."""

    value_fn: Callable[[GatewayEntity], Any]


def _status_label(entity: GatewayEntity) -> str | None:
    code = entity._realtime().get("charger_status")
    if code is None:
        return None
    return STATUS_CODES.get(int(code), f"Code {code}")


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
        # MEASUREMENT (not TOTAL_INCREASING) because the value resets
        # when a new session starts. Long-term statistics for the HA
        # Energy dashboard come from a separate cumulative sensor we
        # add in a follow-on commit.
        state_class=SensorStateClass.MEASUREMENT,
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
    ),
    GatewaySensorEntityDescription(
        key="mains_voltage",
        translation_key="mains_voltage",
        name="Mains voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=_mains_voltage,
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
)


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
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self)
