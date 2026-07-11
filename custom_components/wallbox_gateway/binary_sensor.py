"""Binary sensor platform: ble_connected, charging."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import GatewayCoordinator
from .entity import GatewayEntity


@dataclass(frozen=True, kw_only=True)
class GatewayBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[GatewayEntity], bool | None]


def _ble_connected(entity: GatewayEntity) -> bool:
    return entity._status().get("ble") == "connected"


def _charging(entity: GatewayEntity) -> bool:
    # Charger status 1 = Charging in both the MAX (r_sta) and Zentri (r_dat.st)
    # enums; _charger_status_code() picks the right source per charger family.
    return entity._charger_status_code() == 1


def _schedule_paused(entity: GatewayEntity) -> bool:
    # Manual override active — schedules / Eco-Smart suspended (the Wallbox app's
    # "Resume" state). The gateway computes this authoritatively in /api/status
    # from r_lse.control_mode == 1 (0 = automatic), which is model-agnostic.
    #
    # This used to read r_dat.gen != 0, which was doubly wrong: /api/status never
    # carried a `gen` field (so it was always False), and on the MAX Pro `gen` is
    # accumulated green energy, not an override flag. control_mode is the real
    # signal. (Firmware falls back to gen != 0 only for chargers without r_lse,
    # e.g. Zentri/original Pulsar, where gen genuinely is the flag.)
    return bool(entity._status().get("schedule_paused"))


def _plug_reminder(entity: GatewayEntity) -> bool | None:
    # Charge-reminder engine (#127): ON when a charge is due within the
    # configured lead window and the car is NOT plugged in. The gateway
    # computes it; this is the single entity a notify blueprint binds to.
    val = entity._status().get("plug_reminder")
    return bool(val) if val is not None else None


# Charger status codes that mean a vehicle is plugged in. Mirrors the
# firmware's WallboxBLE::carConnected() r_dat.st set (+ 19 = locked-with-car
# from r_sta). NB: /api/status "sta_connected" is the gateway's WiFi station
# state, NOT the car — do not use it here.
_CAR_CONNECTED_CODES = frozenset({1, 2, 3, 4, 5, 8, 10, 11, 12, 13, 18, 19})


def _car_connected(entity: GatewayEntity) -> bool | None:
    # Prefer the gateway's own flag if a firmware build exposes it on
    # /api/status; otherwise derive from the live charger status code.
    val = entity._status().get("car_connected")
    if isinstance(val, bool):
        return val
    code = entity._charger_status_code()
    if code is None:
        return None
    return code in _CAR_CONNECTED_CODES


BINARY_SENSORS: tuple[GatewayBinaryDescription, ...] = (
    GatewayBinaryDescription(
        key="ble_connected",
        translation_key="ble_connected",
        name="BLE connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=_ble_connected,
    ),
    GatewayBinaryDescription(
        key="charging",
        translation_key="charging",
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=_charging,
    ),
    GatewayBinaryDescription(
        key="schedule_paused",
        translation_key="schedule_paused",
        name="Schedule paused",
        icon="mdi:calendar-clock",
        value_fn=_schedule_paused,
    ),
    # v0.3.0 parity additions (task #110)
    GatewayBinaryDescription(
        key="power_sharing",
        translation_key="power_sharing",
        name="Dynamic power sharing",
        icon="mdi:transit-connection-variant",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: bool(e._power_sharing()) if e._power_sharing() is not None else None,
    ),
    GatewayBinaryDescription(
        key="phase_switch",
        translation_key="phase_switch",
        name="Phase switch",
        icon="mdi:numeric-3-circle",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: bool(e._phase_switch()) if e._phase_switch() is not None else None,
    ),
    GatewayBinaryDescription(
        key="ble_paused",
        translation_key="ble_paused",
        name="BLE paused",
        icon="mdi:bluetooth-off",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: bool(e._status().get("ble_paused")) if "ble_paused" in e._status() else None,
    ),
    # Charge-reminder engine (#127)
    GatewayBinaryDescription(
        key="plug_reminder",
        translation_key="plug_reminder",
        name="Plug-in reminder",
        icon="mdi:power-plug-off",
        value_fn=_plug_reminder,
    ),
    GatewayBinaryDescription(
        key="car_connected",
        translation_key="car_connected",
        name="Car connected",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=_car_connected,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GatewayCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GatewayBinarySensor(coordinator, description)
        for description in BINARY_SENSORS
    )


class GatewayBinarySensor(GatewayEntity, BinarySensorEntity):
    entity_description: GatewayBinaryDescription

    def __init__(
        self,
        coordinator: GatewayCoordinator,
        description: GatewayBinaryDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self)
