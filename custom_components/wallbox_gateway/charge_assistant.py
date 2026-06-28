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
    async_call_later,
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
    CA_SOLAR_MAX_SOC,
    CA_TAP_PATH,
    CA_AUTOSTART_GRACE_MIN,
    CA_TARGET_AUTOSTART,
    CA_TARGET_PCT,
    CA_TARIFF_BELOW,
    CA_TARIFF_ENTITY,
    CA_TITLE,
    CA_TRIGGERS,
    DOMAIN,
    MAX_CURRENT_A,
    MIN_CURRENT_A,
    MODE_OFF,
    MODE_REMINDER,
    MODE_SMART_SOLAR,
    MODE_SOLAR,
    MODE_TARGET,
    CA_WINDOW_ENABLED,
    CA_WINDOW_START,
    CA_WINDOW_END,
    CA_WINDOW_OVERRUN,
    CA_WINDOW_PRESTART,
    CA_WINDOW_COST_WARN,
    TRIG_ARRIVAL,
    TRIG_LEAD,
    TRIG_NIGHTLY,
    TRIG_TARIFF,
    TRIG_SOLAR,
    CA_SOLAR_REMIND_KW,
    CA_HOME_ENTITY,
)
from . import ca_config
from . import charge_window
from . import price_planner
from .charge_guards import derive_surplus, effective_target, price_allows_charge
from .charger_control import WallboxGatewayCharger
from .schedule_arbiter import NativeScheduleArbiter

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = (None, "", "unknown", "unavailable")
_MAX_ESCALATIONS = 3
_SNOOZE_MINUTES = 60
# Target-SOC auto-start deadband: once we've stopped at target, don't auto-
# restart until SOC has fallen this far below target (prevents flapping at the
# cap). Only applies AFTER reaching target this session — a fresh plug-in below
# target uses the small initial margin so it always starts.
_TARGET_DEADBAND = 5
_INITIAL_START_MARGIN = 1
# Some chargers (original/Zentri Pulsar, older Plus firmware) can silently drop
# a Stop. On finish we verify the stop actually took via charge-state readback
# and retry, only declaring "done" once charging has genuinely ceased. The first
# check waits longer than a poll cycle so a normal power ramp-down + the ~10s
# coordinator poll don't read as "still charging" and false-alarm.
_FINISH_STOP_RETRIES = 3
_FINISH_VERIFY_DELAY_S = 18
# Power above which we treat the charger as GENUINELY still charging during a
# finish-verify (kW). A charger that accepted Stop drops to ~0 within seconds; a
# ramp-down tail / stale poll can briefly show a fraction of a kW, which must not
# be mistaken for "ignored the Stop".
_FINISH_STILL_CHARGING_KW = 1.0
# An owner-tagged start overrides the charger's Eco-Smart / Solar-Only pause for
# the session (like a manual start in the official app). But some chargers RE-
# queue it a beat later (Eco-Smart re-asserts when there's no solar at night), so
# the start doesn't hold. After starting we verify it actually took and re-assert
# a few times — mirroring how a manual start "sticks" — before warning the user.
_START_VERIFY_DELAY_S = 12
_START_ASSERT_RETRIES = 3

# Charge-control arbitration (see esp32-wallbox docs/control-owner.md). The
# gateway's control_owner says who may autonomously drive charging; the acting
# modes run only when it equals our id. After a manual (or other-controller)
# command we stand down for a cooldown so we never fight the user.
_OWNER = "integration"
_MANUAL_OVERRIDE_COOLDOWN_S = 1800  # 30 min

# Acting strategies (drive start/stop/current). Reminder is a notify-only layer,
# not an acting strategy, so it's excluded here.
_ACTING = (MODE_TARGET, MODE_SOLAR, MODE_SMART_SOLAR)


class ChargeAssistant:
    """Runs the configured charge-assist behaviour for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._opts: dict = {}
        # Reminder LAYER config (composable model): the plug-in-reminder settings,
        # which may live flat (legacy mode==reminder) or nested under 'reminder'
        # for an acting strategy. Resolved in async_start via ca_config.
        self._rem: dict = {}
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
        # Solar-available reminder edge: True once we've nudged for the current
        # surplus episode; re-armed when surplus drops back below the threshold.
        self._solar_reminded = False
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
        # One-shot guard for the "charging outside your cheap window" cost
        # warning (re-armed once we're back inside the window / not charging).
        self._cost_warned = False
        # Auto-start grace period: a pending start the user can still cancel.
        # _grace_unsub cancels the scheduled fire; _grace_pending holds the
        # context ({"reason", "target"}) for the notification + the fire.
        self._grace_unsub = None
        self._grace_pending: dict | None = None
        # Forced grid-override session: True between a forced start and its
        # finish, so _finish_charge knows to hand control back (resume-if-paused).
        self._managed = False
        # True once a forced charge has reached target this plug-in session — the
        # 5% anti-flap deadband then gates re-starts. Reset on unplug so a fresh
        # plug-in below target always starts (no surprise "won't charge").
        self._reached_target = False
        # "Not now" on a grace nudge holds auto-start off until this time.
        self._autostart_suppress_until: datetime | None = None
        # Finish-verification: a stop we issued at target but haven't yet
        # confirmed actually stopped the charge (some chargers drop a Stop).
        self._finishing: dict | None = None
        self._finish_unsub = None
        # Start-verification: a forced start we issued but haven't confirmed the
        # charger actually held (Eco-Smart can re-queue it). Re-asserts a few
        # times, like a manual start in the official app, before warning.
        self._starting: dict | None = None
        self._start_unsub = None

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
        # Composable model: an acting STRATEGY + an independent reminder LAYER.
        # Legacy mode=='reminder' migrates to strategy 'off' + reminder layer.
        strategy = ca_config.strategy_of(self._opts)
        self._rem = ca_config.reminder_config(self._opts)
        _LOGGER.debug(
            "Charge Assistant: async_start for %s — strategy=%r reminder=%s",
            self.entry.title, strategy, bool(self._rem.get(CA_TRIGGERS)),
        )
        # Auto-resolve the gateway's own entities for this entry (shared).
        self._charge_switch = self._opts.get(CA_CHARGE_SWITCH) or self._own_entity(
            "charging", "switch"
        )
        self._next_charge = self._own_entity("next_scheduled_charge", "sensor")
        self._connected_entity = self._own_entity("car_connected", "binary_sensor")
        # Acting strategy.
        if strategy == MODE_TARGET:
            await self._start_target()
        elif strategy == MODE_SOLAR:
            await self._start_solar()
        elif strategy == MODE_SMART_SOLAR:
            await self._start_smart_solar()
        # Plug-in reminder layer — independent, runs on top of ANY strategy.
        if self._rem.get(CA_TRIGGERS):
            await self._start_reminder()

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
        if ca_config.strategy_of(self._opts) not in _ACTING:
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

        acting = ca_config.strategy_of(self._opts) in _ACTING
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
        triggers = self._rem.get(CA_TRIGGERS) or []
        if not triggers:
            _LOGGER.warning("Charge Assistant: reminder layer on but no triggers selected")
            return

        # The Start/Snooze/Skip buttons fire mobile_app_notification_action.
        self._unsubs.append(
            self.hass.bus.async_listen("mobile_app_notification_action", self._on_action)
        )

        if TRIG_ARRIVAL in triggers and (pe := self._rem.get(CA_ARRIVAL_ENTITY)):
            self._unsubs.append(
                async_track_state_change_event(self.hass, [pe], self._on_arrival)
            )
        if TRIG_NIGHTLY in triggers:
            h, m, s = _parse_hms(self._rem.get(CA_NIGHTLY_TIME, "20:00:00"))
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
        if TRIG_TARIFF in triggers and (pre := self._rem.get(CA_TARIFF_ENTITY)):
            self._unsubs.append(
                async_track_state_change_event(self.hass, [pre], self._on_price)
            )
        if TRIG_SOLAR in triggers:
            if not self._surplus_configured():
                _LOGGER.warning(
                    "Charge Assistant: 'Solar available' reminder on but no surplus "
                    "source configured — set one in the solar settings"
                )
            else:
                # Surplus is noisy + entity-driven → re-check on a steady tick so a
                # rising-edge nudge fires once per surplus episode.
                self._unsubs.append(
                    async_track_time_interval(
                        self.hass, self._on_solar_remind_tick, timedelta(seconds=60)
                    )
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
        # Cancel any pending auto-start countdown / finish-verify / start-verify
        # so none can fire post-teardown.
        self._cancel_grace("assistant stopping")
        self._cancel_finish()
        self._cancel_start_verify()
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
        below = self._rem.get(CA_TARIFF_BELOW)
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
        lead_h = float(self._rem.get(CA_LEAD_HOURS, 0) or 0)
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
    # Solar-available reminder ("plug in — there's free solar")
    # ------------------------------------------------------------------
    def _solar_remind_threshold(self) -> float:
        """Surplus level (in the surplus sensor's units) that triggers the nudge.
        Defaults to the strategy's charge-start level, else 1.4."""
        for key, src in ((CA_SOLAR_REMIND_KW, self._rem), (CA_SURPLUS_START, self._opts)):
            v = src.get(key)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return 1.4

    @callback
    def _on_solar_remind_tick(self, now: datetime) -> None:
        self._eval_solar_remind()

    def _eval_solar_remind(self) -> None:
        """Rising-edge 'solar available — plug in' nudge. Fires once when surplus
        rises to/above the threshold, re-arms when it clearly drops. The
        unplugged / home / quiet / SOC / suppression checks are in _maybe_remind."""
        surplus = self._surplus_value()
        if surplus is None:
            return
        thr = self._solar_remind_threshold()
        if surplus >= thr:
            if not self._solar_reminded:
                self._solar_reminded = True
                self._maybe_remind("solar")
        elif surplus < thr * 0.7:
            self._solar_reminded = False   # re-arm once surplus clearly drops

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
        # Departure / cheapest are time-driven; autostart needs a steady tick too
        # so a plug-in that's stable across an HA restart (no SOC/plug edge to
        # react to) still gets evaluated and started.
        if self._departure_active() or cheapest or self._opts.get(CA_TARGET_AUTOSTART):
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
        # The SOC / plug entities may not be loaded yet at startup (their
        # integration can come up after us), and plain autostart has no later
        # SOC edge to react to — re-check shortly once everything's settled.
        self._unsubs.append(
            async_call_later(self.hass, 20, lambda _now: self._eval_target())
        )

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
            # Charge cap reached — stop if we're charging, hand control back.
            self._cost_warned = False
            self._cancel_grace("target reached")
            if charging:
                _LOGGER.info("Charge Assistant: SOC %.0f%% >= target %.0f%% — stopping charge", soc, target)
                self._finish_charge(soc, target)
            return
        # Below target — start/stop decisions from here.
        if self._plugged_in() is not True:
            self._cancel_grace("car unplugged")
            self._reached_target = False   # fresh session next plug-in
            return

        # Window governs: when a cheap window is enabled it BOUNDS grid charging
        # — we start just-in-time to finish by the window end, stop at the window
        # end (unless overrun), and use the departure deadline only as a fallback
        # (pre-start / overrun). This must run even while charging so the window-
        # end stop fires, so it's handled before the plain "already charging" bail.
        if self._opts.get(CA_WINDOW_ENABLED):
            self._eval_target_windowed(soc, target, charging)
            return

        # No window — legacy behaviour (start only; the cap stop is handled above).
        if charging:
            return
        if self._departure_active():
            # Just-in-time: start only once it's late enough to finish by departure.
            if self._jit_should_start(soc, target):
                self._begin_charge(f"for your {self._opts.get(CA_DEPARTURE)} departure", soc, target)
        elif self._opts.get(CA_TARGET_AUTOSTART) and soc <= target - (
            _TARGET_DEADBAND if self._reached_target else _INITIAL_START_MARGIN
        ):
            # Fresh plug-in starts on any real gap; the wide 5% deadband only
            # gates re-starts after we've already hit target (true anti-flap).
            if self._price_blocks():
                self._note_standby(None)
                return  # above the price cap — wait for a cheaper price
            self._begin_charge("smart charge to target", soc, target)

    def _eval_target_windowed(self, soc: float, target: float, charging: bool) -> None:
        """Target charging when a cheap window is enabled — the window BOUNDS the
        charge. We charge as late as possible to finish by the window END (just-
        in-time within the cheap hours), stop at the window end (unless overrun),
        and only pre-start / overrun toward a departure deadline if the user
        enabled those — so the window genuinely caps spend by default."""
        decision = self._window_decision(soc, target)
        if not decision["allow_charge"]:
            # Outside the cheap window and not pre-starting / overrunning → stop
            # our own charge so it never spills into pricier hours.
            self._cancel_grace("outside cheap window")
            if charging and self._we_started:
                _LOGGER.info(
                    "Charge Assistant: cheap window — stopping charge at %.0f%% (target %.0f%%)",
                    soc, target,
                )
                self._finish_charge(soc, target, reached=False, message=(
                    f"Cheap window ended — paused at {soc:.0f}% (target {target:.0f}%). "
                    "Turn on 'keep charging past the window' to finish anyway."
                ))
            return
        # Charging is allowed right now (in-window, or a departure pre-start /
        # overrun). If already charging, keep going — just flag a pricier charge.
        if charging:
            self._maybe_cost_warn(decision, target)
            return
        # Idle and allowed — decide whether it's time to start.
        if not (self._opts.get(CA_TARGET_AUTOSTART) or self._departure_active()):
            return  # manual target mode: never auto-start
        # Anti-flap: after reaching target this session, wait for a real drop.
        margin = _TARGET_DEADBAND if self._reached_target else _INITIAL_START_MARGIN
        if soc > target - margin:
            return
        if self._price_blocks() and decision.get("reason") != "prestart_for_departure":
            self._note_standby(None)
            return  # above the hard price cap — wait (a departure pre-start wins)
        if not self._window_jit_should_start(soc, target, decision):
            return  # in-window but not yet late enough to finish by the window end
        self._maybe_cost_warn(decision, target)
        self._begin_charge("smart charge in your cheap window", soc, target)

    def _window_jit_should_start(self, soc: float, target: float, decision: dict) -> bool:
        """In-window, start as late as possible to still finish target by the
        window END (charge the latest, and for flat off-peak the cheapest, slice).
        Outside the window but allowed (a departure pre-start / overrun), the
        deadline is driving → start now. No energy model → start once in-window."""
        if not decision.get("in_window"):
            return True
        end = charge_window.to_minutes(self._opts.get(CA_WINDOW_END))
        if end is None:
            return True
        try:
            batt = float(self._opts.get(CA_BATTERY_KWH) or 0)
            power = float(self._opts.get(CA_CHARGE_POWER_KW) or 0)
        except (TypeError, ValueError):
            return True
        if batt <= 0 or power <= 0:
            return True
        now_local = dt_util.now()
        now_min = now_local.hour * 60 + now_local.minute
        mins_to_end = (end - now_min) % (24 * 60)
        needed_kwh = max(0.0, (target - soc) / 100.0 * batt)
        mins_needed = int(needed_kwh / power * 60) + 10  # 10-min safety buffer
        return mins_to_end <= mins_needed

    # ------------------------------------------------------------------
    # Next-start estimate (display) — "when will it start charging?"
    # ------------------------------------------------------------------
    def _next_local_minute(self, minute_of_day: int) -> datetime:
        """Next local occurrence of a minute-of-day, as aware UTC."""
        now_local = dt_util.now()
        h, m = divmod(int(minute_of_day), 60)
        cand = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now_local:
            cand = cand + timedelta(days=1)
        return dt_util.as_utc(cand)

    def _minutes_needed(self, soc: float, target: float) -> int | None:
        """Charge time (minutes, +10 buffer) to reach target, or None without an
        energy model."""
        try:
            batt = float(self._opts.get(CA_BATTERY_KWH) or 0)
            power = float(self._opts.get(CA_CHARGE_POWER_KW) or 0)
        except (TypeError, ValueError):
            return None
        if batt <= 0 or power <= 0:
            return None
        needed_kwh = max(0.0, (target - soc) / 100.0 * batt)
        return int(needed_kwh / power * 60) + 10

    def _planned_start_dt(self, soc: float, target: float) -> tuple[datetime | None, str]:
        """Clock time the windowed / departure plan will start (aware UTC), or
        (None, reason) when it would start immediately or has no time model."""
        opts = self._opts
        mins_needed = self._minutes_needed(soc, target)
        if mins_needed is None:
            return None, ""
        now = dt_util.utcnow()
        if opts.get(CA_WINDOW_ENABLED):
            end = charge_window.to_minutes(opts.get(CA_WINDOW_END))
            start = charge_window.to_minutes(opts.get(CA_WINDOW_START))
            if end is None or start is None:
                return None, ""
            reason = f"to reach {target:.0f}% by {opts.get(CA_WINDOW_END)}"
            start_dt = self._next_local_minute(end) - timedelta(minutes=mins_needed)
            window_open = self._next_local_minute(start)
            if start_dt < window_open <= self._next_local_minute(end):
                start_dt = window_open   # can't fit before the window opens
            return (start_dt if start_dt > now else None), reason
        if self._departure_active():
            dep = self._next_departure()
            if dep is None:
                return None, ""
            reason = f"for your {opts.get(CA_DEPARTURE)} departure"
            start_dt = dep - timedelta(minutes=mins_needed)
            return (start_dt if start_dt > now else None), reason
        return None, ""

    def next_start_estimate(self) -> dict:
        """Best estimate of when the assistant will next START charging, for the
        UI. Returns {state, time (aware UTC | None), reason}. Never raises."""
        try:
            opts = self._opts
            strat = ca_config.strategy_of(opts)
            if strat not in _ACTING:
                return {"state": "off", "time": None, "reason": "Charge Assistant is off"}
            if self._is_charging():
                return {"state": "charging", "time": None, "reason": "charging now"}
            soc = self._read_float(opts.get(CA_SOC_ENTITY))
            target = self._target_pct()
            if soc is not None and soc >= target:
                return {"state": "target_reached", "time": None,
                        "reason": f"at target ({soc:.0f}% ≥ {target:.0f}%)"}
            if strat == MODE_SOLAR:
                return {"state": "solar", "time": None,
                        "reason": "when there's spare solar"}
            if not opts.get(CA_TARGET_AUTOSTART) and not self._departure_active() \
                    and not opts.get(CA_WINDOW_ENABLED):
                return {"state": "manual", "time": None, "reason": "tap Start to charge"}
            start_dt, reason = (None, "")
            if soc is not None:
                start_dt, reason = self._planned_start_dt(soc, target)
            if start_dt is not None:
                return {"state": "scheduled", "time": start_dt, "reason": reason}
            # No future clock time → starts as soon as conditions allow.
            plugged = self._plugged_in()
            why = reason or ("as soon as you plug in" if plugged is not True else "ready to start now")
            return {"state": "due", "time": None, "reason": why}
        except Exception:  # noqa: BLE001 — display helper must never crash
            _LOGGER.debug("Charge Assistant: next_start_estimate failed", exc_info=True)
            return {"state": "unknown", "time": None, "reason": ""}

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
        # Cap reached — stop only our own charge, hand control back.
        if soc >= target:
            self._cancel_grace("target reached")
            if charging and self._we_started:
                _LOGGER.info("Charge Assistant: cheapest — reached target %.0f%%, stopping", target)
                self._finish_charge(soc, target)
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
                self._begin_charge("cheap-rate window", soc, target)
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
                self._begin_charge("for your departure", soc, target)
        elif self._opts.get(CA_TARGET_AUTOSTART) and soc <= target - _TARGET_DEADBAND:
            self._begin_charge("smart charge to target", soc, target)

    def _is_charging(self) -> bool:
        """True if the charger is actively charging. Checks the binary sensor,
        the control switch, AND the live charge power — the binary sensor can
        lag 'off' for a tick after a start / config reload, which would make the
        stop-at-target step wrongly think there's nothing to stop."""
        ent = getattr(self, "_charging_sensor", None)
        if ent and (st := self.hass.states.get(ent)) and st.state not in _UNAVAILABLE:
            if st.state == "on":
                return True
        if self._charge_switch and (sw := self.hass.states.get(self._charge_switch)) and sw.state == "on":
            return True
        # Robust fallback: the gateway's live charge power (r_dat.cp).
        coord = self._coordinator()
        if coord is not None and getattr(coord, "data", None):
            rt = coord.data.get("charger_realtime") or {}
            try:
                if float(rt.get("cp") or 0) > 0.05:
                    return True
            except (TypeError, ValueError):
                pass
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

    def _stop_and_handback(self) -> None:
        """Stop our charge AND clear any lingering Eco/schedule pause so the
        charger's own Solar + schedule control resumes. Used by the continuously-
        managing modes (solar / smart+solar): when we stop, we're yielding, so we
        must not leave the charger paused (which would block native solar too)."""
        self._set_charging(False)
        self.hass.async_create_task(self._resume_if_paused())

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
    # Managed grid-override session: grace period + Eco/schedule handback
    # ------------------------------------------------------------------
    def _grace_minutes(self) -> int:
        try:
            return max(0, int(self._opts.get(CA_AUTOSTART_GRACE_MIN) or 0))
        except (TypeError, ValueError):
            return 0

    def _autostart_suppressed(self) -> bool:
        return (
            self._autostart_suppress_until is not None
            and dt_util.utcnow() < self._autostart_suppress_until
        )

    def _begin_charge(self, reason: str, soc: float, target: float) -> None:
        """The single entry point for every auto-start. With a grace period the
        user first gets a 'starting in N min — tap to cancel' nudge; otherwise it
        starts immediately. Idempotent while a countdown is already running."""
        if self._is_charging() or self._grace_pending is not None:
            return
        if self._autostart_suppressed():
            return  # user said 'Not now' recently
        grace = self._grace_minutes()
        if grace <= 0:
            self._do_start(reason, soc, target)
            return
        self._grace_pending = {"reason": reason, "target": target}
        self._grace_unsub = async_call_later(self.hass, grace * 60, self._grace_fire)
        _LOGGER.info("Charge Assistant: auto-start scheduled in %d min (%s)", grace, reason)
        self.hass.async_create_task(self._send_notification(
            "grace",
            message_override=(
                f"Charging will start in {grace} min — {reason}. "
                "Tap 'Not now' to hold off, or 'Start now' to begin."
            ),
            action_set="grace",
        ))

    @callback
    def _grace_fire(self, _now=None) -> None:
        """Grace elapsed — start unless the situation changed under us."""
        self._grace_unsub = None
        pending = self._grace_pending
        self._grace_pending = None
        if pending is None:
            return
        ok, _ = self._may_control()
        if not ok or self._plugged_in() is not True or self._is_charging():
            return
        soc = self._read_float(self._opts.get(CA_SOC_ENTITY))
        target = pending.get("target")
        if soc is not None and target is not None and soc >= float(target):
            return
        self._do_start(pending.get("reason", ""), soc or 0.0, target or 0.0)

    def _cancel_grace(self, why: str | None = None) -> None:
        """Cancel a pending auto-start (conditions changed, or user opted out)."""
        if self._grace_unsub is not None:
            try:
                self._grace_unsub()
            except Exception:  # noqa: BLE001
                pass
            self._grace_unsub = None
        if self._grace_pending is not None and why:
            _LOGGER.info("Charge Assistant: auto-start cancelled — %s", why)
        self._grace_pending = None

    def _do_start(self, reason: str, soc: float, target: float) -> None:
        """Begin the forced grid charge and confirm. The owner-tagged start
        overrides the charger's Solar-Only / schedule pause for the session
        (verified live), so we do NOT toggle Eco-Smart — that risks leaving solar
        off if a restore fails. _finish_charge hands control back at the end."""
        self._cancel_finish()         # a new charge supersedes any pending finish
        self._set_charging(True)
        self._managed = True          # mark a forced override → finish hands back
        self._reached_target = False  # fresh charge; not yet at target
        # Verify the start actually held (Eco-Smart can re-queue it) and re-assert.
        self._schedule_start_verify(reason, target)
        msg = (
            f"Charging now — {reason} ({soc:.0f}% → {target:.0f}%)."
            if (soc and target) else f"Charging now — {reason}."
        )
        self.hass.async_create_task(
            self._send_notification("target", message_override=msg)
        )

    # ------------------------------------------------------------------
    # Start-verification: re-assert a charge the charger re-queued (Eco-Smart)
    # ------------------------------------------------------------------
    def _schedule_start_verify(self, reason: str, target: float) -> None:
        self._cancel_start_verify()
        self._starting = {"reason": reason, "target": target, "attempts": 1}
        try:
            self._start_unsub = async_call_later(
                self.hass, _START_VERIFY_DELAY_S, self._verify_start
            )
        except Exception:  # noqa: BLE001 — no event loop (tests); watchdog is best-effort
            self._start_unsub = None

    def _cancel_start_verify(self) -> None:
        """Drop any pending start re-assert (we stopped, finished, or tore down)
        so the watchdog can't fight an intentional stop."""
        if self._start_unsub is not None:
            try:
                self._start_unsub()
            except Exception:  # noqa: BLE001
                pass
            self._start_unsub = None
        self._starting = None

    @callback
    def _verify_start(self, _now=None) -> None:
        """Did the start hold? If charging, done. If we still want it but the
        charger re-queued it (Eco-Smart), re-assert a few times, then warn."""
        self._start_unsub = None
        s = self._starting
        if s is None:
            return
        # Only re-assert a charge we still want running.
        if not self._we_started or self._plugged_in() is not True:
            self._starting = None
            return
        soc = self._read_float(self._opts.get(CA_SOC_ENTITY))
        target = s.get("target")
        if soc is not None and target is not None and soc >= float(target):
            self._starting = None
            return  # already at target — nothing to hold
        if self._is_charging():
            self._starting = None
            return  # start took — done
        if s["attempts"] < _START_ASSERT_RETRIES:
            s["attempts"] += 1
            _LOGGER.warning(
                "Charge Assistant: start didn't hold (Eco-Smart re-queue?) — re-asserting %d/%d",
                s["attempts"], _START_ASSERT_RETRIES,
            )
            if self._is_paused():
                # Clear the sticky Eco/schedule pause first, then re-issue start.
                self.hass.async_create_task(self._resume_native())
            self._set_charging(True)   # re-issue the owner-tagged start (overrides Eco)
            try:
                self._start_unsub = async_call_later(
                    self.hass, _START_VERIFY_DELAY_S, self._verify_start
                )
            except Exception:  # noqa: BLE001
                self._start_unsub = None
            return
        # Charger keeps re-queuing it — give up and tell the user.
        self._starting = None
        _LOGGER.error(
            "Charge Assistant: charge wouldn't hold after %d attempts (Eco-Smart override?)",
            _START_ASSERT_RETRIES,
        )
        self.hass.async_create_task(self._send_notification("target", message_override=(
            "Tried to start charging but the charger keeps re-queuing it "
            "(Eco-Smart / Solar-Only may be on). You may need to start it from the app."
        )))

    async def _resume_native(self) -> None:
        """Clear the charger's sticky Eco/schedule pause (gen flag) so a re-
        asserted start isn't immediately re-queued."""
        coord = self._coordinator()
        if coord is None:
            return
        try:
            await WallboxGatewayCharger(coord).resume()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Charge Assistant: resume before re-assert failed: %s", err)

    def _is_paused(self) -> bool:
        """Charger's sticky manual-override / pause flag (r_dat.gen != 0). When
        set, the charger's own Solar + schedule loops are held off."""
        coord = self._coordinator()
        if coord is None or not getattr(coord, "data", None):
            return False
        raw = coord.data.get("raw_status") or {}
        rt = coord.data.get("charger_realtime") or {}
        gen = raw.get("gen", rt.get("gen"))
        try:
            return (gen or 0) != 0
        except TypeError:
            return False

    async def _resume_if_paused(self) -> None:
        """Hand control back to the charger's own Solar + schedule loops after a
        forced charge — but ONLY if it's actually still paused/overridden. A
        clean stop normally leaves it armed (gen=0); a blind ``resume`` would
        just restart charging, so we check the pause flag first."""
        if not self._is_paused():
            _LOGGER.debug("Charge Assistant: finish — charger armed (gen=0), no resume needed")
            return
        coord = self._coordinator()
        if coord is None:
            return
        try:
            await WallboxGatewayCharger(coord).resume()
            _LOGGER.info("Charge Assistant: finish — charger was paused, resumed native control")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Charge Assistant: resume failed: %s", err)

    def _finish_charge(self, soc: float, target: float, *,
                       reached: bool = True, message: str | None = None) -> None:
        """Stop a charge we're managing — at the SOC target (``reached``) or when
        the cheap window ends (``reached=False``). We don't trust the Stop blindly
        (some chargers drop it): issue it, then VERIFY via readback and retry,
        only declaring done + handing control back once charging has actually
        ceased. ``message`` overrides the completion notification."""
        if reached:
            self._reached_target = True
        self._cancel_start_verify()   # a stop supersedes any pending re-assert
        if self._finishing is not None:
            return  # already finishing — let the verify loop run
        self._set_charging(False)
        self._finishing = {"soc": soc, "target": target, "attempts": 1, "message": message}
        self._schedule_finish_verify()

    def _schedule_finish_verify(self) -> None:
        if self._finish_unsub is not None:
            try:
                self._finish_unsub()
            except Exception:  # noqa: BLE001
                pass
        # Pull a fresh reading so the verify checks live state, not a stale poll.
        coord = self._coordinator()
        if coord is not None and hasattr(coord, "async_request_refresh"):
            try:
                self.hass.async_create_task(coord.async_request_refresh())
            except Exception:  # noqa: BLE001
                pass
        self._finish_unsub = async_call_later(
            self.hass, _FINISH_VERIFY_DELAY_S, self._verify_finish
        )

    def _finish_still_charging(self) -> bool:
        """Stricter 'still charging' than _is_charging, for finish-verify only. A
        charger that accepted Stop drops to ~0 within seconds, but the power shows
        a ramp-down tail and the coordinator only polls ~10s, so the binary sensor
        / switch lag 'on'. Only a clearly-significant charge power counts as
        'ignored the Stop' — otherwise a tail / poll lag would false-alarm."""
        coord = self._coordinator()
        if coord is None or not getattr(coord, "data", None):
            return False
        rt = coord.data.get("charger_realtime") or {}
        try:
            return float(rt.get("cp") or 0) > _FINISH_STILL_CHARGING_KW
        except (TypeError, ValueError):
            return False

    def _cancel_finish(self) -> None:
        """Drop any pending finish-verify (a new charge supersedes it, or we're
        tearing down) so the verify loop can't fight a fresh start."""
        if self._finish_unsub is not None:
            try:
                self._finish_unsub()
            except Exception:  # noqa: BLE001
                pass
            self._finish_unsub = None
        self._finishing = None

    @callback
    def _verify_finish(self, _now=None) -> None:
        """Did the Stop take? If charging has ceased, declare done + hand back;
        if it's still charging, retry the stop up to a limit, then warn."""
        self._finish_unsub = None
        f = self._finishing
        if f is None:
            return
        if not self._finish_still_charging():
            # Confirmed stopped — now it's safe to hand control back + notify.
            # Always clear a lingering Eco/schedule pause (idempotent — no-op if
            # not paused) so we never leave the charger stuck unable to run its
            # own Solar/schedules, even for a manual-but-owner charge.
            self._finishing = None
            self._managed = False
            self.hass.async_create_task(self._resume_if_paused())
            self.hass.async_create_task(self._send_notification("target", message_override=(
                f.get("message") or f"Charged to {f['soc']:.0f}% (target {f['target']:.0f}%)."
            )))
            return
        if f["attempts"] < _FINISH_STOP_RETRIES:
            f["attempts"] += 1
            _LOGGER.warning(
                "Charge Assistant: Stop didn't take at target — retry %d/%d",
                f["attempts"], _FINISH_STOP_RETRIES,
            )
            self._set_charging(False)
            self._schedule_finish_verify()
            return
        # Charger keeps ignoring Stop — give up retrying and tell the user.
        self._finishing = None
        _LOGGER.error(
            "Charge Assistant: charger did not accept Stop after %d attempts at %.0f%%",
            f["attempts"], f["soc"],
        )
        self.hass.async_create_task(self._send_notification("target", message_override=(
            f"Reached {f['target']:.0f}% but the charger didn't accept Stop — "
            "you may need to stop it manually."
        )))

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

    def _solar_ceiling(self) -> float:
        """Absolute SOC ceiling for FREE solar charging. The SOC target only caps
        grid top-up; solar keeps filling beyond it up to this cap so we never
        waste surplus. Defaults to 100% (grab all available solar)."""
        try:
            v = self._opts.get(CA_SOLAR_MAX_SOC)
            return float(v) if v not in (None, "") else 100.0
        except (TypeError, ValueError):
            return 100.0

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
                self._stop_and_handback()   # hand back so native Solar/schedules resume
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
    # Allowed charging window (manual cheap-hours restriction)
    # ------------------------------------------------------------------
    def _window_decision(self, soc: float | None, target: float | None) -> dict:
        """Evaluate the manual allowed-window policy for *grid* charging right
        now. Disabled window → always allowed. The departure deadline feeds the
        pre-start relaxation; overrun lets a charge finish past the window."""
        if not self._opts.get(CA_WINDOW_ENABLED):
            return {"in_window": True, "allow_charge": True,
                    "reason": "no_window", "cost_warn": False}
        now_local = dt_util.now()
        now_min = now_local.hour * 60 + now_local.minute
        mins_to_dep = mins_needed = None
        dep = self._next_departure()
        if dep is not None:
            mins_to_dep = max(0, int((dep - dt_util.utcnow()).total_seconds() // 60))
            if soc is not None and target is not None:
                try:
                    batt = float(self._opts.get(CA_BATTERY_KWH) or 0)
                    power = float(self._opts.get(CA_CHARGE_POWER_KW) or 0)
                    if batt > 0 and power > 0:
                        needed_kwh = max(0.0, (target - soc) / 100.0 * batt)
                        mins_needed = int(needed_kwh / power * 60) + 10  # 10-min buffer
                except (TypeError, ValueError):
                    pass
        return charge_window.evaluate(
            now_min,
            start=self._opts.get(CA_WINDOW_START),
            end=self._opts.get(CA_WINDOW_END),
            overrun=bool(self._opts.get(CA_WINDOW_OVERRUN)),
            prestart=bool(self._opts.get(CA_WINDOW_PRESTART)),
            target_met=(soc is not None and target is not None and soc >= target),
            minutes_to_departure=mins_to_dep,
            minutes_needed=mins_needed,
        )

    def _maybe_cost_warn(self, decision: dict, target: float | None) -> None:
        """Notify once when a charge is running OUTSIDE the cheap window (a
        pre-start or overrun), so the user is aware and can choose to stop it.
        Re-armed when we're back inside the window / not charging."""
        if not (self._opts.get(CA_WINDOW_COST_WARN) and decision.get("cost_warn")):
            return
        if self._cost_warned:
            return
        self._cost_warned = True
        why = "to be ready by departure" if decision.get("reason") == "prestart_for_departure" \
            else "to finish the charge"
        tgt = f" toward {target:.0f}%" if isinstance(target, (int, float)) else ""
        self.hass.async_create_task(self._send_notification("window", message_override=(
            f"Heads up: charging is running outside your cheap window {why}{tgt} — "
            "a pricier rate. Stop it from the dashboard if you'd rather wait."
        )))

    # ------------------------------------------------------------------
    # Smart + Solar (composable combined strategy)
    # ------------------------------------------------------------------
    async def _start_smart_solar(self) -> None:
        """Solar-first: charge from surplus whenever it's available, and top up
        from grid only inside the allowed window (or to reach target by
        departure). Stops at the SOC target."""
        if not self._surplus_configured():
            _LOGGER.warning("Charge Assistant: smart+solar needs a surplus source")
            return
        if not self._charge_switch:
            _LOGGER.warning("Charge Assistant: smart+solar couldn't find the charging switch")
            return
        self._charging_sensor = self._own_entity("charging", "binary_sensor")
        watch = []
        if (soc := self._opts.get(CA_SOC_ENTITY)):
            watch.append(soc)
        if self._connected_entity:
            watch.append(self._connected_entity)
        if watch:
            self._unsubs.append(
                async_track_state_change_event(self.hass, watch, self._on_smart_solar_change)
            )
        # Solar is noisy + the window/departure are time-driven → steady poll.
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_smart_solar_tick, timedelta(seconds=60))
        )
        _LOGGER.info(
            "Charge Assistant: smart+solar active — target %s%% departure=%s surplus=%s window=%s",
            self._opts.get(CA_TARGET_PCT, 80), self._opts.get(CA_DEPARTURE) or "off",
            self._surplus_source(), bool(self._opts.get(CA_WINDOW_ENABLED)),
        )
        self._eval_smart_solar()

    @callback
    def _on_smart_solar_tick(self, now: datetime) -> None:
        self._eval_smart_solar()

    @callback
    def _on_smart_solar_change(self, event: Event) -> None:
        self._eval_smart_solar()

    def _eval_smart_solar(self) -> None:
        ok, reason = self._may_control()
        if not ok:
            self._note_standby(reason)
            return
        self._note_standby(None)
        soc = self._read_float(self._opts.get(CA_SOC_ENTITY))
        target = self._target_pct()
        charging = self._is_charging()

        # Solar availability up-front: the SOC target caps GRID top-up, but free
        # solar should keep filling the battery past it — so we need to know
        # whether there's surplus before deciding the target is "reached".
        surplus = self._surplus_value()
        try:
            start_at = float(self._opts.get(CA_SURPLUS_START, 1.4) or 0)
        except (TypeError, ValueError):
            start_at = 0.0
        have_solar = surplus is not None and surplus >= start_at
        plugged = self._plugged_in() is True

        # At/above the grid target: don't waste free solar. Keep charging from
        # surplus up to the (higher) solar ceiling; only stop when it's grid, or
        # the surplus is gone, or we've hit the absolute solar cap.
        if soc is not None and soc >= target:
            ceiling = self._solar_ceiling()
            if have_solar and plugged and soc < ceiling:
                if not charging:
                    _LOGGER.info(
                        "Charge Assistant: smart+solar — above target %.0f%% but solar surplus, grabbing it",
                        soc,
                    )
                    self._set_charging(True)
                    self.hass.async_create_task(self._send_notification("smart_solar", message_override=(
                        f"Charging from spare solar above your {target:.0f}% target (now {soc:.0f}%)."
                    )))
                self._eval_dynamic_current(self._is_charging())
                return
            # No spare solar (or at the solar cap) → enforce the grid target.
            self._cost_warned = False
            if charging and self._we_started:
                _LOGGER.info("Charge Assistant: smart+solar reached %.0f%% — stopping (no spare solar)", soc)
                self._stop_and_handback()   # hand back so native Solar/schedules resume
                self.hass.async_create_task(self._send_notification("smart_solar", message_override=(
                    f"Charging stopped — reached {soc:.0f}% (target {target:.0f}%)."
                )))
            return
        if not plugged:
            return

        # Below target — solar is free (charge on any surplus, ignoring the
        # window); grid top-up is gated by the cheap window (with pre-start /
        # overrun for the departure deadline).
        decision = self._window_decision(soc, target)
        grid_ok = decision["allow_charge"] and not self._price_blocks()
        want = have_solar or grid_ok

        if want:
            if not charging:
                src = "solar surplus" if have_solar else "the cheap window"
                _LOGGER.info("Charge Assistant: smart+solar starting (%s)", src)
                self._set_charging(True)
                if not have_solar:
                    self._maybe_cost_warn(decision, target)
                self.hass.async_create_task(self._send_notification("smart_solar", message_override=(
                    f"Charging started from {src}."
                )))
            elif not have_solar:
                # Already charging from grid — warn if we've slipped outside the window.
                self._maybe_cost_warn(decision, target)
        elif charging and self._we_started:
            _LOGGER.info("Charge Assistant: smart+solar — no surplus and outside window, pausing")
            self._stop_and_handback()   # hand back so native Solar/schedules resume
        if decision.get("in_window"):
            self._cost_warned = False

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
        if not self._home_ok():
            _LOGGER.debug("Charge Assistant: %s skipped — not home", source)
            return
        if not self._soc_skip_ok():
            _LOGGER.debug("Charge Assistant: %s skipped — SOC at/above threshold", source)
            return
        if not self._quiet_ok():
            _LOGGER.debug("Charge Assistant: %s skipped — quiet hours", source)
            return
        if self._rem.get(CA_ONLY_IF_SCHEDULED) and not self._charge_within():
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
        mins = int(self._rem.get(CA_ESCALATE_MIN, 0) or 0)
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

    def _plan_clause(self) -> str | None:
        """One line describing what the *assistant* will do once the car is
        plugged in — so a reminder promises the assistant's actual plan, not the
        charger's stale native-schedule time. Returns None when no acting
        strategy is configured (reminder-only / native schedule runs — the caller
        falls back to the native next-charge time, which is correct there)."""
        opts = self._opts
        strat = ca_config.strategy_of(opts)
        # Plug-aware: a plug-in reminder normally only fires when unplugged, but
        # the wording must never contradict reality (forced test, or the car was
        # plugged in right at reminder time).
        try:
            plugged = self._plugged_in() is True
        except Exception:  # noqa: BLE001 — never let state-read break a message
            plugged = False
        win = ""
        if opts.get(CA_WINDOW_ENABLED) and opts.get(CA_WINDOW_START) and opts.get(CA_WINDOW_END):
            win = f" in the {opts[CA_WINDOW_START]}–{opts[CA_WINDOW_END]} window"
        tgt = opts.get(CA_TARGET_PCT)
        try:
            tgt_s = f" to {int(float(tgt))}%" if tgt else ""
        except (TypeError, ValueError):
            tgt_s = ""
        if strat == MODE_TARGET:
            if opts.get(CA_TARGET_AUTOSTART):
                if win:
                    return f"will charge{tgt_s}{win}"
                when = " now that it's plugged in" if plugged else " as soon as you plug in"
                return f"will charge{tgt_s}{when}"
            if plugged:
                return f"plugged in — tap Start to charge{tgt_s}"
            return f"plug in, then tap Start to charge{tgt_s}"
        if strat == MODE_SOLAR:
            return "will charge from spare solar when there's a surplus"
        if strat == MODE_SMART_SOLAR:
            return f"will use solar first, topping up from grid{win} to reach{tgt_s or ' your target'}"
        return None

    async def _send_notification(
        self, source: str = "reminder", message_override: str | None = None,
        action_set: str | None = None,
    ) -> None:
        try:
            # Charge-event alerts (message_override set) come from the acting
            # strategy's own notify config; plug-in-reminder nudges come from the
            # reminder LAYER. They can target different services.
            cfg = self._opts if message_override is not None else self._rem
            raw = cfg.get(CA_NOTIFY_SERVICE) or ""
            # One or more notify services, comma-separated (the GUI stores
            # multiple targets joined by commas).
            services = [s.strip() for s in str(raw).split(",") if s.strip() and "." in s]
            if not services:
                if message_override is not None:
                    return  # acting-strategy notify is optional — silently skip
                msg = f"no valid notify service configured (got {raw!r})"
                _LOGGER.warning("Charge Assistant: %s", msg)
                self._last_result = msg
                return
            data: dict = {}
            if message_override is not None:
                # Acting-strategy status message — informational. The grace nudge
                # is the exception: it carries override buttons so the user can
                # cancel or start immediately before the countdown fires.
                message = message_override
                if action_set == "grace":
                    data["actions"] = [
                        {"action": CA_START_ACTION, "title": "Start now"},
                        {"action": CA_SNOOZE_ACTION, "title": "Not now"},
                    ]
            else:
                # Plug-aware default base line so the whole message never
                # contradicts itself (a custom message is left untouched).
                try:
                    plugged = self._plugged_in() is True
                except Exception:  # noqa: BLE001
                    plugged = False
                if source == "solar" and not cfg.get(CA_MESSAGE):
                    # Solar-available nudge — say WHY (free solar) when no custom
                    # message is set.
                    message = ("☀️ Solar is flowing and your car isn't plugged in — "
                               "plug in to charge for free.")
                else:
                    message = cfg.get(CA_MESSAGE) or (
                        "Your car is plugged in." if plugged
                        else "Your car isn't plugged in — plug it in to charge."
                    )
                soc_entity = cfg.get(CA_SOC_ENTITY)
                if soc_entity and (st := self.hass.states.get(soc_entity)) and st.state not in _UNAVAILABLE:
                    message = f"{message} · battery {st.state}%"
                plan = self._plan_clause()
                if plan:
                    message = f"{message} · {plan}"
                elif self._next_charge and (nc := self.hass.states.get(self._next_charge)) and nc.state not in _UNAVAILABLE:
                    dt = dt_util.parse_datetime(nc.state)
                    if dt:
                        message = f"{message} · charge {dt_util.as_local(dt).strftime('%a %H:%M')}"
                if cfg.get(CA_ACTIONABLE, True):
                    # Keep titles short — phone notification actions truncate.
                    actions = []
                    if self._charge_switch:
                        actions.append({"action": CA_START_ACTION, "title": "Start now"})
                    actions.append({"action": CA_SNOOZE_ACTION, "title": "Snooze 1h"})
                    actions.append({"action": CA_SKIP_ACTION, "title": "Skip"})
                    data["actions"] = actions
                if cfg.get(CA_TAP_PATH):
                    data["clickAction"] = cfg[CA_TAP_PATH]

            payload = {"title": cfg.get(CA_TITLE) or "Wallbox", "message": message}
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
            # During a grace countdown, "Start now" skips the wait and begins.
            if self._grace_pending is not None:
                pending = self._grace_pending
                self._cancel_grace("user tapped Start now")
                soc = self._read_float(self._opts.get(CA_SOC_ENTITY)) or 0.0
                self._do_start(pending.get("reason", ""), soc, pending.get("target") or 0.0)
            elif self._charge_switch:
                _LOGGER.info("Charge Assistant: 'Start now' -> %s", self._charge_switch)
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": self._charge_switch}, blocking=False
                    )
                )
        elif action == CA_SNOOZE_ACTION:
            # During a grace countdown, "Not now" holds auto-start off for a while.
            if self._grace_pending is not None or self._grace_unsub is not None:
                self._cancel_grace("user tapped Not now")
                self._autostart_suppress_until = dt_util.utcnow() + timedelta(minutes=_SNOOZE_MINUTES)
                _LOGGER.info("Charge Assistant: auto-start held off until %s", self._autostart_suppress_until)
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

    def _home_ok(self) -> bool:
        """'Only when home' condition: True if no home gate is set, or the chosen
        presence entity is `home`. Unknown presence doesn't suppress."""
        eid = self._rem.get(CA_HOME_ENTITY)
        if not eid:
            return True
        st = self.hass.states.get(eid)
        if st is None or st.state in _UNAVAILABLE:
            return True
        return st.state == "home"

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
        window_h = float(self._rem.get(CA_SCHEDULED_WITHIN_H, 12) or 12)
        delta = (dt - dt_util.utcnow()).total_seconds()
        return 0 <= delta <= window_h * 3600

    def _soc_skip_ok(self) -> bool:
        """True = ok to notify. False only when a FRESH reading is >= skip%."""
        soc_entity = self._rem.get(CA_SOC_ENTITY)
        if not soc_entity:
            return True
        st = self.hass.states.get(soc_entity)
        if st is None or st.state in _UNAVAILABLE:
            return True
        try:
            soc = float(st.state)
        except (TypeError, ValueError):
            return True
        max_age = float(self._rem.get(CA_SOC_MAX_AGE, 60) or 0)
        fresh = True
        if max_age > 0:
            age_min = (dt_util.utcnow() - st.last_updated).total_seconds() / 60
            fresh = age_min <= max_age
        threshold = float(self._rem.get(CA_SKIP_ABOVE, 80) or 100)
        return not (fresh and soc >= threshold)

    def _quiet_ok(self) -> bool:
        start = str(self._rem.get(CA_QUIET_START, "00:00:00"))
        end = str(self._rem.get(CA_QUIET_END, "00:00:00"))
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
        self._rem = ca_config.reminder_config(self._opts)
        self._charge_switch = self._opts.get(CA_CHARGE_SWITCH) or self._own_entity(
            "charging", "switch"
        )
        self._next_charge = self._own_entity("next_scheduled_charge", "sensor")
        _LOGGER.info(
            "Charge Assistant: TEST notification requested (notify=%s)",
            self._rem.get(CA_NOTIFY_SERVICE) or self._opts.get(CA_NOTIFY_SERVICE),
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
