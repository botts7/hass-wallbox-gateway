"""Guided Charge Assistant — native controller.

Reminder mode: nudge the user to plug in. The wizard (config_flow.py
Options flow) lets the user pick any combination of triggers, all sharing
the same conditions + notification — no automation, no helpers. The
controller runs it in Python.

Triggers:
  * arrival  — a presence entity (person/device_tracker) turns ``home``
  * nightly  — a fixed time of day
  * lead     — N hours before the next scheduled charge
  * tariff   — an electricity-price entity drops to/below a threshold

Conditions (gate every trigger):
  * car not plugged in (binary_sensor.car_connected, from sta_connected)
  * optional SOC skip (don't nag if already charged enough)
  * quiet hours
  * optional "only if a charge is scheduled within X hours"

Notification: optional Start now / Snooze / Skip action buttons and an
optional escalate (re-remind if still unplugged).

Scheduled / Prompt modes are reserved for later phases.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CA_ACTIONABLE,
    CA_ARRIVAL_ENTITY,
    CA_CHARGE_SWITCH,
    CA_ESCALATE_MIN,
    CA_KEY,
    CA_LEAD_HOURS,
    CA_MESSAGE,
    CA_MODE,
    CA_NIGHTLY_TIME,
    CA_NOTIFY_SERVICE,
    CA_ONLY_IF_SCHEDULED,
    CA_QUIET_END,
    CA_QUIET_START,
    CA_SCHEDULED_WITHIN_H,
    CA_SKIP_ABOVE,
    CA_SKIP_ACTION,
    CA_SNOOZE_ACTION,
    CA_SOC_ENTITY,
    CA_SOC_MAX_AGE,
    CA_BATTERY_KWH,
    CA_CHARGE_POWER_KW,
    CA_DEPARTURE,
    CA_START_ACTION,
    CA_SURPLUS_DEBOUNCE,
    CA_SURPLUS_ENTITY,
    CA_SURPLUS_START,
    CA_SURPLUS_STOP,
    CA_TAP_PATH,
    CA_TARGET_AUTOSTART,
    CA_TARGET_PCT,
    CA_TARIFF_BELOW,
    CA_TARIFF_ENTITY,
    CA_TITLE,
    CA_TRIGGERS,
    DOMAIN,
    MODE_REMINDER,
    MODE_SOLAR,
    MODE_TARGET,
    TRIG_ARRIVAL,
    TRIG_LEAD,
    TRIG_NIGHTLY,
    TRIG_TARIFF,
)

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = (None, "", "unknown", "unavailable")
_MAX_ESCALATIONS = 3
_SNOOZE_MINUTES = 60
# Target-SOC auto-start deadband: once stopped at target, don't auto-restart
# until SOC has fallen this far below target (prevents flapping around the cap).
_TARGET_DEADBAND = 5

# Charge-control arbitration (see esp32-wallbox docs/control-owner.md). The
# gateway's control_owner says who may autonomously drive charging; the acting
# modes run only when it equals our id. After a manual (or other-controller)
# command we stand down for a cooldown so we never fight the user.
_OWNER = "integration"
_MANUAL_OVERRIDE_COOLDOWN_S = 1800  # 30 min


class ChargeAssistant:
    """Runs the configured charge-assist behaviour for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._opts: dict = {}
        self._unsubs: list = []
        self._lead_unsub = None
        self._escalate_unsub = None
        self._charge_switch: str | None = None
        self._next_charge: str | None = None
        self._connected_entity: str | None = None
        self._charging_sensor: str | None = None
        # Solar-surplus debounce timestamps.
        self._surplus_since: datetime | None = None
        self._deficit_since: datetime | None = None
        # Suppression windows set by the Snooze / Skip notification actions.
        self._suppress_until: datetime | None = None
        self._escalations_left = 0
        self._last_result: str = "(not run)"
        # Most recent reason the acting modes stood down (gateway owner isn't
        # us / manual override). Surfaced by the diagnostic sensor + Repair.
        self._standby_reason: str | None = None

    # ------------------------------------------------------------------
    # Own-entity resolution
    # ------------------------------------------------------------------
    def _own_entity(self, key: str, domain: str) -> str | None:
        """This config entry's own entity, by unique-id key + domain.

        Entities are unique_id = "<serial>_<key>", registered to this entry,
        so we find the gateway's own car_connected / charging /
        next_scheduled_charge without the user picking them.
        """
        reg = er.async_get(self.hass)
        for ent in er.async_entries_for_config_entry(reg, self.entry.entry_id):
            if ent.domain == domain and ent.unique_id.endswith(f"_{key}"):
                return ent.entity_id
        return None

    # ------------------------------------------------------------------
    # Control arbitration (gateway control_owner + manual override)
    # ------------------------------------------------------------------
    def _coordinator(self):
        return self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)

    def _status(self) -> dict:
        coord = self._coordinator()
        if coord is None or not coord.data:
            return {}
        return coord.data.get("raw_status") or {}

    def control_owner(self) -> str:
        """The gateway's configured charge-control owner ('' if unknown)."""
        return str(self._status().get("control_owner") or "")

    @property
    def standby_reason(self) -> str | None:
        """Why the acting modes are standing down, or None when in control."""
        return self._standby_reason

    def _may_control(self) -> tuple[bool, str]:
        """(allowed, reason). We may drive charging only when the gateway owner
        is us AND there's no recent manual/other-controller command. An empty
        owner (old firmware / status not yet loaded) is treated as permissive
        for backward compatibility."""
        owner = self.control_owner()
        if owner and owner != _OWNER:
            return False, f"gateway control owner is '{owner}', not the integration"
        st = self._status()
        by = str(st.get("last_command_by") or "")
        try:
            age = int(st.get("last_command_age_s"))
        except (TypeError, ValueError):
            age = -1
        if by and by != _OWNER and 0 <= age < _MANUAL_OVERRIDE_COOLDOWN_S:
            return False, f"manual override {age}s ago (by '{by}') — backing off"
        return True, ""

    def _note_standby(self, reason: str | None) -> None:
        if reason != self._standby_reason:
            if reason:
                _LOGGER.info("Charge Assistant: standing by — %s", reason)
            self._standby_reason = reason

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        """Wire up listeners for the configured mode."""
        self._opts = dict(self.entry.options.get(CA_KEY) or {})
        mode = self._opts.get(CA_MODE)
        _LOGGER.debug("Charge Assistant: async_start for %s — mode=%r", self.entry.title, mode)
        # Auto-resolve the gateway's own entities for this entry (shared).
        self._charge_switch = self._opts.get(CA_CHARGE_SWITCH) or self._own_entity(
            "charging", "switch"
        )
        self._next_charge = self._own_entity("next_scheduled_charge", "sensor")
        self._connected_entity = self._own_entity("car_connected", "binary_sensor")
        if mode == MODE_REMINDER:
            await self._start_reminder()
        elif mode == MODE_TARGET:
            await self._start_target()
        elif mode == MODE_SOLAR:
            await self._start_solar()

    async def _start_reminder(self) -> None:
        triggers = self._opts.get(CA_TRIGGERS) or []
        if not triggers:
            _LOGGER.warning("Charge Assistant: reminder mode on but no triggers selected")
            return

        # The Start/Snooze/Skip buttons fire mobile_app_notification_action.
        self._unsubs.append(
            self.hass.bus.async_listen("mobile_app_notification_action", self._on_action)
        )

        if TRIG_ARRIVAL in triggers and (pe := self._opts.get(CA_ARRIVAL_ENTITY)):
            self._unsubs.append(
                async_track_state_change_event(self.hass, [pe], self._on_arrival)
            )
        if TRIG_NIGHTLY in triggers:
            h, m, s = _parse_hms(self._opts.get(CA_NIGHTLY_TIME, "20:00:00"))
            self._unsubs.append(
                async_track_time_change(self.hass, self._on_nightly, hour=h, minute=m, second=s)
            )
        if TRIG_LEAD in triggers and self._next_charge:
            # Re-evaluate the lead alarm whenever the next-charge time moves.
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [self._next_charge], self._on_next_charge_change
                )
            )
            self._schedule_lead()
        if TRIG_TARIFF in triggers and (pre := self._opts.get(CA_TARIFF_ENTITY)):
            self._unsubs.append(
                async_track_state_change_event(self.hass, [pre], self._on_price)
            )

        _LOGGER.info(
            "Charge Assistant: reminder active on %s (triggers=%s)",
            self.entry.title,
            ",".join(triggers),
        )

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for handle in (self._lead_unsub, self._escalate_unsub):
            if handle:
                handle()
        self._lead_unsub = None
        self._escalate_unsub = None

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------
    @callback
    def _on_arrival(self, event: Event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or new.state != "home":
            return
        if old is not None and old.state == "home":
            return  # only the ->home edge
        self._maybe_remind("arrival")

    @callback
    def _on_nightly(self, now: datetime) -> None:
        self._maybe_remind("nightly")

    @callback
    def _on_price(self, event: Event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        below = self._opts.get(CA_TARIFF_BELOW)
        if below is None or new is None or new.state in _UNAVAILABLE:
            return
        try:
            new_v = float(new.state)
            thr = float(below)
        except (TypeError, ValueError):
            return
        old_v = None
        if old is not None and old.state not in _UNAVAILABLE:
            try:
                old_v = float(old.state)
            except (TypeError, ValueError):
                old_v = None
        # Fire only on the downward crossing into the cheap window.
        if new_v <= thr and (old_v is None or old_v > thr):
            self._maybe_remind("tariff")

    @callback
    def _on_next_charge_change(self, event: Event) -> None:
        self._schedule_lead()

    def _schedule_lead(self) -> None:
        """(Re)arm the lead-time alarm from the next-charge sensor."""
        if self._lead_unsub:
            self._lead_unsub()
            self._lead_unsub = None
        if not self._next_charge:
            return
        st = self.hass.states.get(self._next_charge)
        if st is None or st.state in _UNAVAILABLE:
            return
        charge_dt = dt_util.parse_datetime(st.state)
        if charge_dt is None:
            return
        lead_h = float(self._opts.get(CA_LEAD_HOURS, 0) or 0)
        fire_at = charge_dt - timedelta(hours=lead_h)
        now = dt_util.utcnow()
        if fire_at <= now < charge_dt:
            # Lead point already passed but charge still ahead — check soon.
            fire_at = now + timedelta(seconds=5)
        if fire_at <= now:
            return  # charge already in the past
        self._lead_unsub = async_track_point_in_time(
            self.hass, self._on_lead, fire_at
        )

    @callback
    def _on_lead(self, now: datetime) -> None:
        self._lead_unsub = None
        self._maybe_remind("lead")

    # ------------------------------------------------------------------
    # Target-SOC (smart charge) mode
    # ------------------------------------------------------------------
    async def _start_target(self) -> None:
        soc_entity = self._opts.get(CA_SOC_ENTITY)
        if not soc_entity:
            _LOGGER.warning("Charge Assistant: target mode needs a battery-level sensor")
            return
        if not self._charge_switch:
            _LOGGER.warning("Charge Assistant: target mode couldn't find the charging switch")
            return
        self._charging_sensor = self._own_entity("charging", "binary_sensor")
        # Re-evaluate whenever SOC moves or the cable is plugged/unplugged.
        watch = [soc_entity]
        if self._connected_entity:
            watch.append(self._connected_entity)
        self._unsubs.append(
            async_track_state_change_event(self.hass, watch, self._on_target_change)
        )
        # Departure targeting is time-driven (not just SOC-driven), so poll too.
        if self._departure_active():
            self._unsubs.append(
                async_track_time_interval(self.hass, self._on_target_tick, timedelta(minutes=5))
            )
        _LOGGER.info(
            "Charge Assistant: target mode active — cap %s%% on %s (autostart=%s, departure=%s)",
            self._opts.get(CA_TARGET_PCT, 80),
            soc_entity,
            bool(self._opts.get(CA_TARGET_AUTOSTART)),
            self._opts.get(CA_DEPARTURE) or "off",
        )
        # Evaluate once at startup in case we're already at/over target.
        self._eval_target()

    @callback
    def _on_target_tick(self, now: datetime) -> None:
        self._eval_target()

    def _departure_active(self) -> bool:
        """True when a valid departure target is configured."""
        if not self._opts.get(CA_DEPARTURE):
            return False
        try:
            return float(self._opts.get(CA_BATTERY_KWH) or 0) > 0 and float(
                self._opts.get(CA_CHARGE_POWER_KW) or 0
            ) > 0
        except (TypeError, ValueError):
            return False

    def _next_departure(self) -> datetime | None:
        """Next occurrence of the local departure time, as UTC."""
        val = self._opts.get(CA_DEPARTURE)
        if not val:
            return None
        try:
            parts = str(val).split(":")
            h, m = int(parts[0]), int(parts[1])
        except (TypeError, ValueError):
            return None
        now_local = dt_util.now()
        dep = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if dep <= now_local:
            dep = dep + timedelta(days=1)
        return dt_util.as_utc(dep)

    def _jit_should_start(self, soc: float, target: float) -> bool:
        """True once it's late enough that charging must start to hit target by departure."""
        dep = self._next_departure()
        if dep is None:
            return False
        try:
            batt = float(self._opts.get(CA_BATTERY_KWH) or 0)
            power = float(self._opts.get(CA_CHARGE_POWER_KW) or 0)
        except (TypeError, ValueError):
            return False
        if batt <= 0 or power <= 0:
            return False
        needed_kwh = max(0.0, (target - soc) / 100.0 * batt)
        duration = timedelta(hours=needed_kwh / power)
        latest_start = dep - duration - timedelta(minutes=10)  # 10-min safety buffer
        return dt_util.utcnow() >= latest_start

    @callback
    def _on_target_change(self, event: Event) -> None:
        self._eval_target()

    def _eval_target(self) -> None:
        ok, reason = self._may_control()
        if not ok:
            self._note_standby(reason)
            return
        self._note_standby(None)
        soc = self._read_float(self._opts.get(CA_SOC_ENTITY))
        if soc is None:
            return
        try:
            target = float(self._opts.get(CA_TARGET_PCT, 80) or 80)
        except (TypeError, ValueError):
            return
        charging = self._is_charging()
        if soc >= target:
            # Charge cap reached — stop if we're charging.
            if charging:
                _LOGGER.info("Charge Assistant: SOC %.0f%% >= target %.0f%% — stopping charge", soc, target)
                self._set_charging(False)
                self.hass.async_create_task(
                    self._send_notification("target", message_override=(
                        f"Charging stopped — reached {soc:.0f}% (target {target:.0f}%)."
                    ))
                )
            return
        # Below target — only consider starting when plugged in and idle.
        if charging or self._plugged_in() is not True:
            return
        if self._departure_active():
            # Just-in-time: start only once it's late enough to finish by departure.
            if self._jit_should_start(soc, target):
                _LOGGER.info("Charge Assistant: departure just-in-time — starting at SOC %.0f%%", soc)
                self._set_charging(True)
                self.hass.async_create_task(
                    self._send_notification("target", message_override=(
                        f"Charging started for your {self._opts.get(CA_DEPARTURE)} departure — {soc:.0f}% to {target:.0f}%."
                    ))
                )
        elif self._opts.get(CA_TARGET_AUTOSTART) and soc <= target - _TARGET_DEADBAND:
            # Immediate auto-start (deadband stops flapping at the cap).
            _LOGGER.info("Charge Assistant: SOC %.0f%% below target %.0f%% — starting charge", soc, target)
            self._set_charging(True)
            self.hass.async_create_task(
                self._send_notification("target", message_override=(
                    f"Charging started — {soc:.0f}% (target {target:.0f}%)."
                ))
            )

    def _is_charging(self) -> bool:
        """True if the charger reports it's actively charging."""
        ent = getattr(self, "_charging_sensor", None)
        if ent and (st := self.hass.states.get(ent)) and st.state not in _UNAVAILABLE:
            return st.state == "on"
        # Fall back to the control switch's commanded state.
        if self._charge_switch and (sw := self.hass.states.get(self._charge_switch)):
            return sw.state == "on"
        return False

    def _set_charging(self, on: bool) -> None:
        """Autonomously start/stop charging — owner-tagged so the gateway
        records us as the commander (and we don't mistake it for a manual
        override). Sends straight to the gateway's /api/command rather than
        the switch entity, so the tag survives. Defensive gate: never act
        when we're not the owner (the eval already gated, but belt + braces)."""
        ok, reason = self._may_control()
        if not ok:
            self._note_standby(reason)
            return
        coord = self._coordinator()
        if coord is None:
            return
        self.hass.async_create_task(self._send_command(coord, "start" if on else "stop"))

    async def _send_command(self, coord, action: str) -> None:
        try:
            await coord.client.command(
                {"action": action, "owner": _OWNER, "wait": "5000"}
            )
        except Exception as err:  # noqa: BLE001 — never crash the eval loop
            _LOGGER.warning("Charge Assistant: %s command failed: %s", action, err)

    def _read_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in _UNAVAILABLE:
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Solar-surplus mode
    # ------------------------------------------------------------------
    async def _start_solar(self) -> None:
        surplus = self._opts.get(CA_SURPLUS_ENTITY)
        if not surplus:
            _LOGGER.warning("Charge Assistant: solar mode needs a surplus power sensor")
            return
        if not self._charge_switch:
            _LOGGER.warning("Charge Assistant: solar mode couldn't find the charging switch")
            return
        self._charging_sensor = self._own_entity("charging", "binary_sensor")
        # Debounced poll: solar is noisy, so evaluate on a steady cadence and
        # require the condition to hold `debounce` minutes before acting.
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_solar_tick, timedelta(seconds=60))
        )
        _LOGGER.info(
            "Charge Assistant: solar mode active — start>=%s stop<=%s on %s (debounce %s min)",
            self._opts.get(CA_SURPLUS_START), self._opts.get(CA_SURPLUS_STOP),
            surplus, self._opts.get(CA_SURPLUS_DEBOUNCE, 3),
        )
        self._eval_solar()

    @callback
    def _on_solar_tick(self, now: datetime) -> None:
        self._eval_solar()

    def _eval_solar(self) -> None:
        ok, reason = self._may_control()
        if not ok:
            self._note_standby(reason)
            return
        self._note_standby(None)
        surplus = self._read_float(self._opts.get(CA_SURPLUS_ENTITY))
        if surplus is None:
            return
        try:
            start_at = float(self._opts.get(CA_SURPLUS_START, 1.4) or 0)
            stop_at = float(self._opts.get(CA_SURPLUS_STOP, 0) or 0)
            debounce = float(self._opts.get(CA_SURPLUS_DEBOUNCE, 3) or 0)
        except (TypeError, ValueError):
            return
        now = dt_util.utcnow()
        charging = self._is_charging()
        held = lambda since: since is not None and (now - since).total_seconds() >= debounce * 60

        if surplus >= start_at:
            self._deficit_since = None
            if self._surplus_since is None:
                self._surplus_since = now
            if not charging and held(self._surplus_since) and self._plugged_in() is True:
                _LOGGER.info("Charge Assistant: solar surplus %.2f held >= %.2f — starting", surplus, start_at)
                self._set_charging(True)
                self._surplus_since = None
                self.hass.async_create_task(self._send_notification("solar", message_override=(
                    f"Solar charging started — surplus {surplus:.2f}."
                )))
        elif surplus <= stop_at:
            self._surplus_since = None
            if self._deficit_since is None:
                self._deficit_since = now
            if charging and held(self._deficit_since):
                _LOGGER.info("Charge Assistant: solar surplus %.2f below %.2f — stopping", surplus, stop_at)
                self._set_charging(False)
                self._deficit_since = None
                self.hass.async_create_task(self._send_notification("solar", message_override=(
                    f"Solar charging paused — surplus dropped to {surplus:.2f}."
                )))
        else:
            # Hysteresis band — hold current state, reset both timers.
            self._surplus_since = None
            self._deficit_since = None

    # ------------------------------------------------------------------
    # Decision + notification
    # ------------------------------------------------------------------
    @callback
    def _maybe_remind(self, source: str) -> None:
        if self._suppressed():
            _LOGGER.debug("Charge Assistant: %s suppressed (snooze/skip)", source)
            return
        plugged = self._plugged_in()
        if plugged is not False:
            _LOGGER.debug("Charge Assistant: %s — car connected/unknown, no nudge", source)
            return
        if not self._soc_skip_ok():
            _LOGGER.debug("Charge Assistant: %s skipped — SOC at/above threshold", source)
            return
        if not self._quiet_ok():
            _LOGGER.debug("Charge Assistant: %s skipped — quiet hours", source)
            return
        if self._opts.get(CA_ONLY_IF_SCHEDULED) and not self._charge_within():
            _LOGGER.debug("Charge Assistant: %s skipped — no charge scheduled in window", source)
            return
        _LOGGER.info("Charge Assistant: reminding (trigger=%s)", source)
        self._escalations_left = _MAX_ESCALATIONS
        self.hass.async_create_task(self._send_notification(source))
        self._arm_escalation()

    def _arm_escalation(self) -> None:
        if self._escalate_unsub:
            self._escalate_unsub()
            self._escalate_unsub = None
        mins = int(self._opts.get(CA_ESCALATE_MIN, 0) or 0)
        if mins <= 0 or self._escalations_left <= 0:
            return
        self._escalate_unsub = async_track_point_in_time(
            self.hass, self._on_escalate, dt_util.utcnow() + timedelta(minutes=mins)
        )

    @callback
    def _on_escalate(self, now: datetime) -> None:
        self._escalate_unsub = None
        if self._suppressed() or self._plugged_in() is not False:
            return  # plugged in or snoozed — stop nagging
        self._escalations_left -= 1
        self.hass.async_create_task(self._send_notification("reminder"))
        self._arm_escalation()

    async def _send_notification(self, source: str = "reminder", message_override: str | None = None) -> None:
        try:
            service = self._opts.get(CA_NOTIFY_SERVICE)
            if not service or "." not in service:
                if message_override is not None:
                    return  # target-mode notify is optional — silently skip
                msg = f"no valid notify service configured (got {service!r})"
                _LOGGER.warning("Charge Assistant: %s", msg)
                self._last_result = msg
                return
            domain, name = service.split(".", 1)
            data: dict = {}
            if message_override is not None:
                # Target-mode status message — informational, no action buttons.
                message = message_override
            else:
                message = self._opts.get(CA_MESSAGE) or "Your car isn't plugged in — plug it in to charge."
                soc_entity = self._opts.get(CA_SOC_ENTITY)
                if soc_entity and (st := self.hass.states.get(soc_entity)) and st.state not in _UNAVAILABLE:
                    message = f"{message} · battery {st.state}%"
                if self._next_charge and (nc := self.hass.states.get(self._next_charge)) and nc.state not in _UNAVAILABLE:
                    dt = dt_util.parse_datetime(nc.state)
                    if dt:
                        message = f"{message} · charge {dt_util.as_local(dt).strftime('%a %H:%M')}"
                if self._opts.get(CA_ACTIONABLE, True):
                    actions = []
                    if self._charge_switch:
                        actions.append({"action": CA_START_ACTION, "title": "Start charging now"})
                    actions.append({"action": CA_SNOOZE_ACTION, "title": "Snooze 1h"})
                    actions.append({"action": CA_SKIP_ACTION, "title": "Skip tonight"})
                    data["actions"] = actions
                if self._opts.get(CA_TAP_PATH):
                    data["clickAction"] = self._opts[CA_TAP_PATH]

            payload = {"title": self._opts.get(CA_TITLE) or "Wallbox", "message": message}
            if data:
                payload["data"] = data
            _LOGGER.debug("Charge Assistant: calling %s.%s payload=%s", domain, name, payload)
            await self.hass.services.async_call(domain, name, payload, blocking=True)
            _LOGGER.info("Charge Assistant: notification sent via %s.%s (trigger=%s)", domain, name, source)
            self._last_result = f"sent OK via {domain}.{name}\npayload={payload}"
        except Exception as err:  # noqa: BLE001 — don't let a notify failure crash the loop
            _LOGGER.exception("Charge Assistant: _send_notification FAILED")
            self._last_result = f"FAILED: {type(err).__name__}: {err}"

    # ------------------------------------------------------------------
    # Notification action buttons
    # ------------------------------------------------------------------
    @callback
    def _on_action(self, event: Event) -> None:
        action = event.data.get("action")
        if action == CA_START_ACTION:
            if self._charge_switch:
                _LOGGER.info("Charge Assistant: 'Start now' -> %s", self._charge_switch)
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": self._charge_switch}, blocking=False
                    )
                )
        elif action == CA_SNOOZE_ACTION:
            self._suppress_until = dt_util.utcnow() + timedelta(minutes=_SNOOZE_MINUTES)
            self._cancel_escalation()
            _LOGGER.info("Charge Assistant: snoozed until %s", self._suppress_until)
        elif action == CA_SKIP_ACTION:
            # Suppress until tomorrow morning (06:00 local).
            now_local = dt_util.now()
            tomorrow = (now_local + timedelta(days=1)).replace(
                hour=6, minute=0, second=0, microsecond=0
            )
            self._suppress_until = dt_util.as_utc(tomorrow)
            self._cancel_escalation()
            _LOGGER.info("Charge Assistant: skipped until %s", self._suppress_until)

    def _cancel_escalation(self) -> None:
        if self._escalate_unsub:
            self._escalate_unsub()
            self._escalate_unsub = None

    # ------------------------------------------------------------------
    # Conditions
    # ------------------------------------------------------------------
    def _plugged_in(self) -> bool | None:
        """True = connected, False = unplugged, None = unknown."""
        if not self._connected_entity:
            return None
        st = self.hass.states.get(self._connected_entity)
        if st is None or st.state in _UNAVAILABLE:
            return None
        return st.state == "on"

    def _suppressed(self) -> bool:
        return self._suppress_until is not None and dt_util.utcnow() < self._suppress_until

    def _charge_within(self) -> bool:
        """True if the next scheduled charge is within the configured window."""
        if not self._next_charge:
            return False
        st = self.hass.states.get(self._next_charge)
        if st is None or st.state in _UNAVAILABLE:
            return False
        dt = dt_util.parse_datetime(st.state)
        if dt is None:
            return False
        window_h = float(self._opts.get(CA_SCHEDULED_WITHIN_H, 12) or 12)
        delta = (dt - dt_util.utcnow()).total_seconds()
        return 0 <= delta <= window_h * 3600

    def _soc_skip_ok(self) -> bool:
        """True = ok to notify. False only when a FRESH reading is >= skip%."""
        soc_entity = self._opts.get(CA_SOC_ENTITY)
        if not soc_entity:
            return True
        st = self.hass.states.get(soc_entity)
        if st is None or st.state in _UNAVAILABLE:
            return True
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

    # ------------------------------------------------------------------
    # On-demand test (wallbox_gateway.test_reminder)
    # ------------------------------------------------------------------
    async def async_test(self) -> None:
        """Fire the reminder notification on demand (ignores conditions).

        Writes the outcome to <config>/wallbox_ca_test.txt for inspection.
        """
        self._opts = dict(self.entry.options.get(CA_KEY) or {})
        self._charge_switch = self._opts.get(CA_CHARGE_SWITCH) or self._own_entity(
            "charging", "switch"
        )
        self._next_charge = self._own_entity("next_scheduled_charge", "sensor")
        _LOGGER.info(
            "Charge Assistant: TEST notification requested (notify=%s)",
            self._opts.get(CA_NOTIFY_SERVICE),
        )
        self._last_result = "(no result recorded)"
        await self._send_notification("test")
        path = self.hass.config.path("wallbox_ca_test.txt")
        await self.hass.async_add_executor_job(self._write_result, path)

    def _write_result(self, path: str) -> None:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._last_result)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Charge Assistant: couldn't write test result file")


def _parse_hms(value: str) -> tuple[int, int, int]:
    """Parse 'HH:MM' or 'HH:MM:SS' into (h, m, s); default 20:00:00."""
    try:
        parts = [int(p) for p in str(value).split(":")]
        h = parts[0] if len(parts) > 0 else 20
        m = parts[1] if len(parts) > 1 else 0
        s = parts[2] if len(parts) > 2 else 0
        return h, m, s
    except (TypeError, ValueError):
        return 20, 0, 0
