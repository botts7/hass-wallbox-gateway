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
    CA_LOAD_LIMIT_W,
    CA_LOAD_POWER_ENTITY,
    CA_MAX_CURRENT,
    CA_MESSAGE,
    CA_MIN_CURRENT,
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
    CA_CHEAPEST,
    CA_DEPARTURE,
    CA_PRICE_CAP,
    CA_PRICE_ENTITY,
    CA_TRIP_TARGET,
    CA_TRIP_UNTIL,
    CA_START_ACTION,
    CA_SOLAR_DYNAMIC,
    CA_SUPPLY_PHASES,
    CA_SUPPLY_VOLTAGE,
    CA_GRID_ENTITY,
    CA_GRID_EXPORT_NEGATIVE,
    CA_LOAD_ENTITY,
    CA_SOLAR_ENTITY,
    CA_SURPLUS_DEBOUNCE,
    CA_SURPLUS_ENTITY,
    CA_SURPLUS_SOURCE,
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
    MAX_CURRENT_A,
    MIN_CURRENT_A,
    MODE_REMINDER,
    MODE_SOLAR,
    MODE_TARGET,
    TRIG_ARRIVAL,
    TRIG_LEAD,
    TRIG_NIGHTLY,
    TRIG_TARIFF,
)
from . import price_planner
from .charge_guards import derive_surplus, effective_target, price_allows_charge
from .charger_control import WallboxGatewayCharger
from .schedule_arbiter import NativeScheduleArbiter

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
        # Native-schedule arbiter + the last SUCCESSFULLY-applied control state.
        # We retry until the gateway actually reflects what we want (BLE may be
        # busy right after an owner change / reboot), so this tracks "applied",
        # not just "intended".
        self._arbiter: NativeScheduleArbiter | None = None
        self._applied_sc: bool | None = None
        self._applying = False
        # Last charge current (A) we commanded — so dynamic control only writes
        # to the charger when the target actually changes (BLE writes are dear).
        self._applied_current: int | None = None
        # Did *we* start the current charge? Cheapest-window only ever stops a
        # charge it started itself — never a manual / app-initiated one.
        self._we_started = False

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

        # Native-schedule arbiter: while we actively control charging, the
        # charger's own schedules are disabled (and restored when we hand back).
        # Driven by ownership changes via a coordinator listener.
        coord = self._coordinator()
        if coord is not None:
            self._arbiter = NativeScheduleArbiter(self.hass, self.entry, coord.client)
            self._unsubs.append(coord.async_add_listener(self._on_coord_update))
            self._update_repair()
            await self._reconcile_schedules()

    # ------------------------------------------------------------------
    # Native-schedule arbitration + ownership reactions
    # ------------------------------------------------------------------
    def _should_control(self) -> bool:
        """True when we're the gateway owner AND in an acting (start/stop) mode."""
        if self._opts.get(CA_MODE) not in (MODE_TARGET, MODE_SOLAR):
            return False
        return self.control_owner() == _OWNER

    async def _reconcile_schedules(self) -> None:
        await self._apply_control(self._should_control())

    async def _apply_control(self, sc: bool) -> None:
        """Drive the arbiter toward the desired control state, retrying until
        the gateway actually applies it (BLE can be busy after an owner change
        / reboot, so a single attempt isn't enough)."""
        if self._arbiter is None or self._applying:
            return
        self._applying = True
        try:
            ok = await self._arbiter.async_reconcile(sc)
            if ok:
                if sc != self._applied_sc:
                    _LOGGER.info(
                        "Charge Assistant: schedule control applied -> %s (owner=%s)",
                        sc, self.control_owner(),
                    )
                self._applied_sc = sc
        finally:
            self._applying = False

    @callback
    def _on_coord_update(self) -> None:
        """Coordinator tick — reconcile whenever the gateway's applied state
        doesn't yet match what we want (covers post-reboot retries)."""
        if self._arbiter is None:
            return
        self._update_repair()
        sc = self._should_control()
        if sc != self._applied_sc:
            self.hass.async_create_task(self._apply_control(sc))

    def _update_repair(self) -> None:
        """Raise/clear an HA Repair when an acting mode is set but we aren't the
        gateway's control owner (so it's obvious why nothing is happening)."""
        from homeassistant.helpers import issue_registry as ir

        acting = self._opts.get(CA_MODE) in (MODE_TARGET, MODE_SOLAR)
        owner = self.control_owner()
        issue_id = f"not_control_owner_{self.entry.entry_id}"
        if acting and owner and owner != _OWNER:
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="not_control_owner",
                translation_placeholders={"owner": owner},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

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
        # Hand control back to the charger's own schedules on teardown (reload
        # or removal) so we never leave a native schedule disabled. async_start
        # re-takes control afterwards if we're still the owner.
        if self._arbiter is not None:
            try:
                await self._arbiter.async_reconcile(False)
            except Exception:  # noqa: BLE001 — teardown must not raise
                _LOGGER.exception("Charge Assistant: schedule restore on stop failed")
        from homeassistant.helpers import issue_registry as ir
        ir.async_delete_issue(self.hass, DOMAIN, f"not_control_owner_{self.entry.entry_id}")

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
        cheapest = self._cheapest_active()
        if cheapest:
            # Re-plan when the price forecast updates.
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [self._opts.get(CA_PRICE_ENTITY)], self._on_target_change
                )
            )
        # Departure targeting and cheapest-window are both time-driven (not just
        # SOC-driven), so poll on a steady cadence too.
        if self._departure_active() or cheapest:
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
        target = self._target_pct()
        charging = self._is_charging()
        if self._cheapest_active():
            self._eval_cheapest(soc, target, charging)
            return
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
            if self._price_blocks():
                self._note_standby(None)
                return  # above the price cap — wait for a cheaper price
            # Immediate auto-start (deadband stops flapping at the cap).
            _LOGGER.info("Charge Assistant: SOC %.0f%% below target %.0f%% — starting charge", soc, target)
            self._set_charging(True)
            self.hass.async_create_task(
                self._send_notification("target", message_override=(
                    f"Charging started — {soc:.0f}% (target {target:.0f}%)."
                ))
            )

    # ------------------------------------------------------------------
    # Cheapest-window planning (Phase 3) — a sub-mode of target charging
    # ------------------------------------------------------------------
    def _cheapest_active(self) -> bool:
        """True when cheapest-window is enabled with the inputs it needs (price
        entity + a battery/power model to size the energy required)."""
        if not self._opts.get(CA_CHEAPEST) or not self._opts.get(CA_PRICE_ENTITY):
            return False
        try:
            return float(self._opts.get(CA_BATTERY_KWH) or 0) > 0 and float(
                self._opts.get(CA_CHARGE_POWER_KW) or 0
            ) > 0
        except (TypeError, ValueError):
            return False

    def _parse_dt(self, v):
        """Parse a forecast timestamp (ISO str or datetime) to aware UTC."""
        if v is None:
            return None
        if isinstance(v, datetime):
            return dt_util.as_utc(v)
        d = dt_util.parse_datetime(str(v))
        return dt_util.as_utc(d) if d else None

    def _parse_dt_local(self, v):
        """Parse a naive local datetime string (trip deadline) to aware UTC."""
        if not v:
            return None
        d = dt_util.parse_datetime(str(v))
        if d is None:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return dt_util.as_utc(d)

    # ------------------------------------------------------------------
    # Battery care + cost cap (Phase 4)
    # ------------------------------------------------------------------
    def _target_pct(self) -> float:
        """The active SOC target: the everyday care target, raised to the trip
        target only until the trip deadline (auto-reverts by time)."""
        return effective_target(
            self._opts.get(CA_TARGET_PCT, 80),
            self._opts.get(CA_TRIP_TARGET),
            self._parse_dt_local(self._opts.get(CA_TRIP_UNTIL)),
            dt_util.utcnow(),
        )

    def _price_blocks(self) -> bool:
        """True when a price cap is set and the current price exceeds it. The
        caller's departure floor overrides this so the car is still ready."""
        cap = self._opts.get(CA_PRICE_CAP)
        if cap in (None, ""):
            return False
        price = self._read_float(self._opts.get(CA_PRICE_ENTITY))
        return not price_allows_charge(price, cap)

    def _eval_cheapest(self, soc: float, target: float, charging: bool) -> None:
        """Charge only during the cheapest forecast hours that still reach
        target by departure. Safety nets: (1) the departure just-in-time floor
        forces charging if cheap hours run short, so the car is always ready;
        (2) we only ever STOP a charge we started ourselves — never a manual
        one; (3) if the price entity has no usable forecast we fall back to the
        plain target/JIT behaviour."""
        # Cap reached — stop only our own charge.
        if soc >= target:
            if charging and self._we_started:
                _LOGGER.info("Charge Assistant: cheapest — reached target %.0f%%, stopping", target)
                self._set_charging(False)
                self.hass.async_create_task(self._send_notification("target", message_override=(
                    f"Charging stopped — reached {soc:.0f}% (target {target:.0f}%)."
                )))
            return

        now = dt_util.utcnow()
        dep = self._next_departure()
        deadline = dep if dep is not None else now + timedelta(hours=24)
        try:
            batt = float(self._opts.get(CA_BATTERY_KWH))
            power = float(self._opts.get(CA_CHARGE_POWER_KW))
        except (TypeError, ValueError):
            return
        energy = max(0.0, (target - soc) / 100.0 * batt)

        pe = self._opts.get(CA_PRICE_ENTITY)
        st = self.hass.states.get(pe) if pe else None
        attrs = dict(st.attributes) if st else {}
        slots = price_planner.parse_forecast(attrs, self._parse_dt, now)
        if not slots:
            # No forecast available — can't optimise on price. Fall back to the
            # departure floor (or autostart) so we still hit target in time.
            self._target_fallback(soc, target, charging, dep is not None)
            return

        plan = price_planner.plan_cheapest(slots, energy, power, now, deadline)
        want = price_planner.is_charge_now(plan, now)
        # Departure floor: if it's now too late to reach target by leaving time,
        # charge regardless of price/window.
        floor = dep is not None and self._jit_should_start(soc, target)
        if floor:
            want = True
        elif want and self._price_blocks():
            want = False    # in a cheap window, but still above the hard price cap

        if want:
            if not charging and self._plugged_in() is True:
                _LOGGER.info("Charge Assistant: cheapest — in a cheap window, starting at %.0f%%", soc)
                self._set_charging(True)
                self.hass.async_create_task(self._send_notification("target", message_override=(
                    f"Charging now — cheap-rate window ({soc:.0f}% → {target:.0f}%)."
                )))
        elif charging and self._we_started:
            # Outside a cheap window — pause, but only our own charge.
            _LOGGER.info("Charge Assistant: cheapest — outside cheap window, pausing")
            self._set_charging(False)
            self.hass.async_create_task(self._send_notification("target", message_override=(
                "Charging paused — waiting for a cheaper window."
            )))

    def _target_fallback(self, soc: float, target: float, charging: bool, has_departure: bool) -> None:
        """Plain target behaviour (JIT if a departure is set, else autostart) —
        used when cheapest-window is on but no forecast is available."""
        if charging or self._plugged_in() is not True:
            return
        if has_departure:
            if self._jit_should_start(soc, target):
                self._set_charging(True)
        elif self._opts.get(CA_TARGET_AUTOSTART) and soc <= target - _TARGET_DEADBAND:
            self._set_charging(True)

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
        if not on:
            # Next start should re-assert the current from scratch.
            self._applied_current = None
            self._we_started = False
        else:
            self._we_started = True
        self.hass.async_create_task(self._send_command(coord, "start" if on else "stop"))

    async def _send_command(self, coord, action: str, value=None) -> None:
        # All charge control goes through the ChargerControl adapter — the seam
        # for supporting non-Wallbox chargers (see charger_control.py).
        charger = WallboxGatewayCharger(coord)
        try:
            if action == "start":
                await charger.start()
            elif action == "stop":
                await charger.stop()
            elif action == "current":
                await charger.set_current(int(value))
            else:
                _LOGGER.warning("Charge Assistant: unknown charger action %s", action)
        except Exception as err:  # noqa: BLE001 — never crash the eval loop
            _LOGGER.warning("Charge Assistant: %s command failed: %s", action, err)

    # ------------------------------------------------------------------
    # Dynamic current control (Phase 2)
    # ------------------------------------------------------------------
    def _current_bounds(self) -> tuple[int, int]:
        """(min, max) charge current the assistant may command, clamped to the
        charger's hardware range. User can narrow but never exceed it."""
        try:
            lo = int(self._opts.get(CA_MIN_CURRENT, MIN_CURRENT_A) or MIN_CURRENT_A)
        except (TypeError, ValueError):
            lo = MIN_CURRENT_A
        try:
            hi = int(self._opts.get(CA_MAX_CURRENT, MAX_CURRENT_A) or MAX_CURRENT_A)
        except (TypeError, ValueError):
            hi = MAX_CURRENT_A
        lo = max(MIN_CURRENT_A, min(lo, MAX_CURRENT_A))
        hi = max(MIN_CURRENT_A, min(hi, MAX_CURRENT_A))
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi

    def _power_to_amps(self, power_w: float) -> float:
        """Convert an available *power* (W) to a per-phase current using the
        configured supply geometry."""
        try:
            volts = float(self._opts.get(CA_SUPPLY_VOLTAGE, 230) or 230)
            phases = float(self._opts.get(CA_SUPPLY_PHASES, 1) or 1)
        except (TypeError, ValueError):
            volts, phases = 230.0, 1.0
        denom = volts * max(phases, 1.0)
        return power_w / denom if denom > 0 else 0.0

    def _set_current(self, amps: float) -> None:
        """Command a charge current (A), clamped to the configured bounds and
        owner-tagged. No-op if it equals the last value we set (avoid needless
        BLE writes) or if we're not allowed to control right now."""
        ok, reason = self._may_control()
        if not ok:
            self._note_standby(reason)
            return
        lo, hi = self._current_bounds()
        target = int(round(max(lo, min(amps, hi))))
        if target == self._applied_current:
            return
        coord = self._coordinator()
        if coord is None:
            return
        _LOGGER.info("Charge Assistant: setting charge current to %d A", target)
        self._applied_current = target
        self.hass.async_create_task(self._send_command(coord, "current", value=target))

    # ------------------------------------------------------------------
    # Surplus source (wizard) — derive surplus from whatever sensors exist
    # ------------------------------------------------------------------
    def _surplus_source(self) -> str:
        return str(self._opts.get(CA_SURPLUS_SOURCE) or "entity")

    def _surplus_value(self) -> float | None:
        """Available solar surplus, per the configured source (direct sensor,
        grid export, or solar − load)."""
        src = self._surplus_source()
        return derive_surplus(
            src,
            surplus=self._read_float(self._opts.get(CA_SURPLUS_ENTITY)),
            grid=self._read_float(self._opts.get(CA_GRID_ENTITY)),
            solar=self._read_float(self._opts.get(CA_SOLAR_ENTITY)),
            load=self._read_float(self._opts.get(CA_LOAD_ENTITY)),
            grid_export_negative=bool(self._opts.get(CA_GRID_EXPORT_NEGATIVE, True)),
        )

    def _surplus_unit_entity(self) -> str | None:
        """Which entity's unit represents the surplus value (for W conversion)."""
        src = self._surplus_source()
        if src == "grid":
            return self._opts.get(CA_GRID_ENTITY)
        if src == "solar_load":
            return self._opts.get(CA_SOLAR_ENTITY)
        return self._opts.get(CA_SURPLUS_ENTITY)

    def _surplus_configured(self) -> bool:
        src = self._surplus_source()
        if src == "grid":
            return bool(self._opts.get(CA_GRID_ENTITY))
        if src == "solar_load":
            return bool(self._opts.get(CA_SOLAR_ENTITY) and self._opts.get(CA_LOAD_ENTITY))
        return bool(self._opts.get(CA_SURPLUS_ENTITY))

    def _to_watts(self, value: float | None) -> float | None:
        """Normalise a value in the surplus source's units to watts. Uses the
        source sensor's unit_of_measurement; falls back to a magnitude heuristic
        (bare numbers below 100 look like kW) only when no unit is published."""
        if value is None:
            return None
        eid = self._surplus_unit_entity()
        st = self.hass.states.get(eid) if eid else None
        unit = ((st.attributes.get("unit_of_measurement") if st else "") or "").lower()
        if "kw" in unit:
            return value * 1000.0
        if "w" in unit:
            return value
        return value * 1000.0 if abs(value) < 100 else value

    def _eval_dynamic_current(self, charging: bool) -> None:
        """Solar-follow current modulation + house-load shedding. Runs on the
        solar tick while charging. Incremental controller: nudge the commanded
        current toward the available surplus (keeping a margin so we don't pull
        from the grid), then clamp it so total house draw stays under the load
        limit. Both are opt-in."""
        dynamic = bool(self._opts.get(CA_SOLAR_DYNAMIC))
        try:
            load_limit = float(self._opts.get(CA_LOAD_LIMIT_W) or 0)
        except (TypeError, ValueError):
            load_limit = 0.0
        if not charging or (not dynamic and load_limit <= 0):
            return
        lo, hi = self._current_bounds()
        base = self._applied_current if self._applied_current is not None else lo
        target = float(base)

        if dynamic:
            surplus_w = self._to_watts(self._surplus_value())
            if surplus_w is not None:
                # Keep a margin equal to the start threshold so steady state
                # sits at "comfortably exporting", not flickering at the edge.
                margin_w = self._to_watts(self._read_float_opt(CA_SURPLUS_START)) or 0.0
                target = base + self._power_to_amps(surplus_w - margin_w)

        if load_limit > 0:
            house_w = self._house_power_w()
            if house_w is not None:
                over_w = house_w - load_limit
                if over_w > 0:
                    # Over the cap — shed at least this many amps off the base.
                    target = min(target, base - self._power_to_amps(over_w))

        self._set_current(target)

    def _house_power_w(self) -> float | None:
        """Total house/grid power in watts for load-balancing. Prefers the
        user-chosen HA sensor (so it works without the charger's Power Boost
        accessory); falls back to the charger's own meter reading."""
        eid = self._opts.get(CA_LOAD_POWER_ENTITY)
        if eid:
            val = self._read_float(eid)
            if val is None:
                return None
            st = self.hass.states.get(eid)
            unit = ((st.attributes.get("unit_of_measurement") if st else "") or "").lower()
            return val * 1000.0 if "kw" in unit else val
        coord = self._coordinator()
        meter = (coord.data.get("meter") if coord and coord.data else {}) or {}
        house_w = meter.get("house_power_w")
        return float(house_w) if isinstance(house_w, (int, float)) else None

    def _read_float_opt(self, key: str) -> float | None:
        """Read a numeric CA option (not an entity) as float, or None."""
        try:
            v = self._opts.get(key)
            return None if v in (None, "") else float(v)
        except (TypeError, ValueError):
            return None

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
        if not self._surplus_configured():
            _LOGGER.warning("Charge Assistant: solar mode needs a surplus source (sensor / grid / solar+load)")
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
            "Charge Assistant: solar mode active — start>=%s stop<=%s source=%s (debounce %s min)",
            self._opts.get(CA_SURPLUS_START), self._opts.get(CA_SURPLUS_STOP),
            self._surplus_source(), self._opts.get(CA_SURPLUS_DEBOUNCE, 3),
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
        surplus = self._surplus_value()
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

        # Once the start/stop decision is made, modulate the current to follow
        # surplus (and shed under the house-load limit) while charging.
        self._eval_dynamic_current(self._is_charging())

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
            raw = self._opts.get(CA_NOTIFY_SERVICE) or ""
            # One or more notify services, comma-separated (the GUI stores
            # multiple targets joined by commas).
            services = [s.strip() for s in str(raw).split(",") if s.strip() and "." in s]
            if not services:
                if message_override is not None:
                    return  # target-mode notify is optional — silently skip
                msg = f"no valid notify service configured (got {raw!r})"
                _LOGGER.warning("Charge Assistant: %s", msg)
                self._last_result = msg
                return
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
            sent = []
            for svc in services:
                domain, name = svc.split(".", 1)
                _LOGGER.debug("Charge Assistant: calling %s.%s payload=%s", domain, name, payload)
                await self.hass.services.async_call(domain, name, payload, blocking=True)
                sent.append(f"{domain}.{name}")
            _LOGGER.info("Charge Assistant: notification sent via %s (trigger=%s)", ", ".join(sent), source)
            self._last_result = f"sent OK via {', '.join(sent)}\npayload={payload}"
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
