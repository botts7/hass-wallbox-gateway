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
    # Charger status 1 = Charging per the BAPI enum in const.STATUS_CODES.
    return entity._realtime().get("charger_status") == 1


def _schedule_paused(entity: GatewayEntity) -> bool:
    # r_dat.gen is the sticky manual-override flag the Wallbox app
    # surfaces as "Schedule paused" / "Solar charging paused":
    #   gen == 0 -> schedule armed (will fire normally)
    #   gen != 0 -> schedule paused (override active, persists across
    #               Start/Stop until the Wallbox app's Resume button
    #               is pressed)
    # Independent of charger_status: a manually-started charge while
    # the schedule is paused will report status=1 (CHARGING) with
    # gen != 0.
    return (entity._status().get("gen") or 0) != 0


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
