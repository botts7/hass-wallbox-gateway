"""Guided Charge Assistant — native controller.

Phase 1: Reminder mode. Configured via the Options flow (see
config_flow.py) and stored in entry.options[CA_KEY]. The controller runs
the logic itself — no user automation, no helpers. Mirrors the Reminder
branch of the charge_assistant blueprint, in Python.

Scheduled / Prompt modes are reserved for later phases.
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    CA_CHARGE_SWITCH,
    CA_KEY,
    CA_MESSAGE,
    CA_NOTIFY_SERVICE,
    CA_QUIET_END,
    CA_QUIET_START,
    CA_REMINDER_ENTITY,
    CA_SKIP_ABOVE,
    CA_SOC_ENTITY,
    CA_SOC_MAX_AGE,
    CA_START_ACTION,
    CA_TAP_PATH,
    CA_TITLE,
    CA_MODE,
    MODE_REMINDER,
)

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = (None, "", "unknown", "unavailable")


class ChargeAssistant:
    """Runs the configured charge-assist behaviour for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._opts: dict = {}
        self._unsubs: list = []
        self._charge_switch: str | None = None
        self._next_charge: str | None = None

    def _own_entity(self, key: str, domain: str) -> str | None:
        """This config entry's own entity, by unique-id key + domain.

        Entities are unique_id = "<serial>_<key>", registered to this entry,
        so we can find the gateway's own plug_reminder / charging /
        next_scheduled_charge without the user picking them.
        """
        reg = er.async_get(self.hass)
        for ent in er.async_entries_for_config_entry(reg, self.entry.entry_id):
            if ent.domain == domain and ent.unique_id.endswith(f"_{key}"):
                return ent.entity_id
        return None

    async def async_start(self) -> None:
        """Wire up listeners for the configured mode."""
        self._opts = dict(self.entry.options.get(CA_KEY) or {})
        mode = self._opts.get(CA_MODE)
        # TEMP diagnostics at WARNING so they show without debug logging.
        _LOGGER.warning("Charge Assistant: async_start for %s — mode=%r", self.entry.title, mode)
        if mode != MODE_REMINDER:
            _LOGGER.warning("Charge Assistant: mode is not 'reminder' — nothing wired")
            return  # off / not-yet-implemented modes do nothing

        # Auto-resolve the gateway's OWN entities for this entry (a stored
        # override is honoured if ever set), so the user never has to pick them.
        reminder_entity = self._opts.get(CA_REMINDER_ENTITY) or self._own_entity(
            "plug_reminder", "binary_sensor"
        )
        self._charge_switch = self._opts.get(CA_CHARGE_SWITCH) or self._own_entity(
            "charging", "switch"
        )
        self._next_charge = self._own_entity("next_scheduled_charge", "sensor")
        if not reminder_entity:
            _LOGGER.warning(
                "Charge Assistant: couldn't find this gateway's plug-in reminder sensor"
            )
            return

        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [reminder_entity], self._on_reminder
            )
        )
        # "Start charging now" button on the notification.
        self._unsubs.append(
            self.hass.bus.async_listen("mobile_app_notification_action", self._on_action)
        )
        _LOGGER.info("Charge Assistant: reminder mode active on %s", reminder_entity)

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def async_test(self) -> None:
        """Fire the reminder notification on demand (ignores conditions).

        Exposed via the wallbox_gateway.test_reminder service so a user can
        confirm the notify path + config without faking entity states.
        """
        self._opts = dict(self.entry.options.get(CA_KEY) or {})
        self._charge_switch = self._opts.get(CA_CHARGE_SWITCH) or self._own_entity(
            "charging", "switch"
        )
        self._next_charge = self._own_entity("next_scheduled_charge", "sensor")
        _LOGGER.warning(
            "Charge Assistant: TEST notification requested (notify=%s)",
            self._opts.get(CA_NOTIFY_SERVICE),
        )
        await self._send_notification()

    # ---- reminder trigger ----

    @callback
    def _on_reminder(self, event: Event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or new.state != "on":
            return
        if old is not None and old.state == "on":
            return  # only the off->on edge
        _LOGGER.info("Charge Assistant: plug-in reminder turned ON")
        if not self._soc_skip_ok():
            _LOGGER.info("Charge Assistant: skipped — battery at/above the skip threshold")
            return
        if not self._quiet_ok():
            _LOGGER.info("Charge Assistant: skipped — quiet hours")
            return
        _LOGGER.info("Charge Assistant: sending reminder notification")
        self.hass.async_create_task(self._send_notification())

    async def _send_notification(self) -> None:
        service = self._opts.get(CA_NOTIFY_SERVICE)
        if not service or "." not in service:
            _LOGGER.warning("Charge Assistant: no valid notify service configured")
            return
        domain, name = service.split(".", 1)
        message = self._opts.get(CA_MESSAGE) or "Your car isn't plugged in — a charge is coming up."
        soc_entity = self._opts.get(CA_SOC_ENTITY)
        if soc_entity and (st := self.hass.states.get(soc_entity)) and st.state not in _UNAVAILABLE:
            message = f"{message} · battery {st.state}%"
        # Append the scheduled time from the gateway's own next-charge sensor.
        if self._next_charge and (nc := self.hass.states.get(self._next_charge)) and nc.state not in _UNAVAILABLE:
            dt = dt_util.parse_datetime(nc.state)
            if dt:
                message = f"{message} · charge {dt_util.as_local(dt).strftime('%a %H:%M')}"

        data: dict = {}
        if self._charge_switch:
            data["actions"] = [
                {"action": CA_START_ACTION, "title": "Start charging now"}
            ]
        if self._opts.get(CA_TAP_PATH):
            data["clickAction"] = self._opts[CA_TAP_PATH]

        payload = {"title": self._opts.get(CA_TITLE) or "Wallbox", "message": message}
        if data:
            payload["data"] = data
        try:
            await self.hass.services.async_call(domain, name, payload, blocking=True)
            _LOGGER.info("Charge Assistant: notification sent via %s.%s", domain, name)
        except Exception:  # noqa: BLE001 — don't let a notify failure crash the loop
            _LOGGER.exception("Charge Assistant: notify call to %s.%s failed", domain, name)

    # ---- "Start now" button ----

    @callback
    def _on_action(self, event: Event) -> None:
        if event.data.get("action") != CA_START_ACTION:
            return
        if not self._charge_switch:
            return
        _LOGGER.info("Charge Assistant: 'Start now' tapped -> turning on %s", self._charge_switch)
        self.hass.async_create_task(
            self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": self._charge_switch}, blocking=False
            )
        )

    # ---- conditions (mirror the blueprint) ----

    def _soc_skip_ok(self) -> bool:
        """True = ok to notify. False only when a FRESH reading is >= skip%."""
        soc_entity = self._opts.get(CA_SOC_ENTITY)
        if not soc_entity:
            return True
        st = self.hass.states.get(soc_entity)
        if st is None or st.state in _UNAVAILABLE:
            return True  # no/dead reading -> don't trust it to suppress
        try:
            soc = float(st.state)
        except (TypeError, ValueError):
            return True
        max_age = float(self._opts.get(CA_SOC_MAX_AGE, 60) or 0)
        fresh = True
        if max_age > 0:
            age_min = (dt_util.utcnow() - st.last_updated).total_seconds() / 60
            fresh = age_min <= max_age
        threshold = float(self._opts.get(CA_SKIP_ABOVE, 80) or 100)
        return not (fresh and soc >= threshold)

    def _quiet_ok(self) -> bool:
        start = str(self._opts.get(CA_QUIET_START, "00:00:00"))
        end = str(self._opts.get(CA_QUIET_END, "00:00:00"))
        if start == end:
            return True
        now = datetime.now().strftime("%H:%M:%S")
        if start < end:
            return not (start <= now < end)
        return not (now >= start or now < end)
