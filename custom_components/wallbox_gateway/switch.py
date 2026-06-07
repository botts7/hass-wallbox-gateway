"""Switch platform for the Wallbox Gateway integration.

Three switches in v0.2:
  - charging         (start / stop via /api/command?action=start|stop)
  - lock             (lock / unlock via /api/command?action=lock|unlock)
  - auto_lock_enabled (s_alo BAPI with the bare-integer shape — seconds
                       window, 0 = off. We write DEFAULT_AUTOLOCK_SECONDS
                       on turn_on, 0 on turn_off; the granular minutes
                       control comes in v0.3 via a number entity.)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_AUTOLOCK_SECONDS, DOMAIN
from .coordinator import GatewayCoordinator
from .entity import GatewayEntity


@dataclass(frozen=True, kw_only=True)
class GatewaySwitchEntityDescription(SwitchEntityDescription):
    """A switch plus the three callables that drive it."""

    value_fn: Callable[[GatewayEntity], bool | None]
    turn_on_fn: Callable[["GatewaySwitch"], Awaitable[Any]]
    turn_off_fn: Callable[["GatewaySwitch"], Awaitable[Any]]


# ---------- value_fn (read current state) ----------

def _charging_value(entity: GatewayEntity) -> bool | None:
    code = entity._realtime().get("charger_status")
    if code is None:
        return None
    # Status code 1 = Charging per const.STATUS_CODES.
    return int(code) == 1


def _lock_value(entity: GatewayEntity) -> bool | None:
    code = entity._realtime().get("charger_status")
    if code is None:
        return None
    # Status code 6 = Locked. Everything else means the charger is not
    # in the lock state. This is the same heuristic the dashboard uses.
    return int(code) == 6


def _autolock_value(entity: GatewayEntity) -> bool | None:
    data = entity._autolock()
    if not data:
        return None
    return bool(data.get("enabled"))


# ---------- turn_on / turn_off (write) ----------

async def _start_charging(switch: "GatewaySwitch") -> Any:
    return await switch.coordinator.client.get(
        "/api/command?action=start&wait=5000"
    )


async def _stop_charging(switch: "GatewaySwitch") -> Any:
    return await switch.coordinator.client.get(
        "/api/command?action=stop&wait=5000"
    )


async def _lock(switch: "GatewaySwitch") -> Any:
    return await switch.coordinator.client.get(
        "/api/command?action=lock&wait=5000"
    )


async def _unlock(switch: "GatewaySwitch") -> Any:
    return await switch.coordinator.client.get(
        "/api/command?action=unlock&wait=5000"
    )


async def _autolock_on(switch: "GatewaySwitch") -> Any:
    # s_alo bare-integer shape: par = seconds (string). Restore the
    # prior known window if we have one, otherwise the safe default.
    prior_seconds = (switch._autolock().get("seconds") or 0)
    seconds = prior_seconds if prior_seconds > 0 else DEFAULT_AUTOLOCK_SECONDS
    return await switch.coordinator.client.bapi(
        "s_alo", par=str(seconds), wait_ms=6000
    )


async def _autolock_off(switch: "GatewaySwitch") -> Any:
    return await switch.coordinator.client.bapi(
        "s_alo", par="0", wait_ms=6000
    )


SWITCHES: tuple[GatewaySwitchEntityDescription, ...] = (
    GatewaySwitchEntityDescription(
        key="charging",
        translation_key="charging",
        name="Charging",
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=_charging_value,
        turn_on_fn=_start_charging,
        turn_off_fn=_stop_charging,
    ),
    GatewaySwitchEntityDescription(
        key="lock",
        translation_key="lock",
        name="Lock",
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=_lock_value,
        turn_on_fn=_lock,
        turn_off_fn=_unlock,
    ),
    GatewaySwitchEntityDescription(
        key="auto_lock_enabled",
        translation_key="auto_lock_enabled",
        name="Auto lock",
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=_autolock_value,
        turn_on_fn=_autolock_on,
        turn_off_fn=_autolock_off,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GatewayCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GatewaySwitch(coordinator, description) for description in SWITCHES
    )


class GatewaySwitch(GatewayEntity, SwitchEntity):
    entity_description: GatewaySwitchEntityDescription

    def __init__(
        self,
        coordinator: GatewayCoordinator,
        description: GatewaySwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.entity_description.turn_on_fn(self)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.entity_description.turn_off_fn(self)
        await self.coordinator.async_request_refresh()
