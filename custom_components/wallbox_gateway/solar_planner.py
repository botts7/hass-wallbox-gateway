"""Plan the night around tomorrow's solar: the Home Assistant side.

Keeps the car's home pattern and the forecast correction in a Store, reads the
forecast (any forecast integration through HA's energy forecast hook, or an
hourly outlook sensor such as Solar Comfort's), and works out tonight's plan
with the pure modules (home_pattern, solar_outlook, overnight_plan).

Every source is optional. With none, or with too little history, the plan
counts no solar and the night charges as before.

- "preview": the plan is shown (sensor, evening note); nothing changes.
- "on": ChargeAssistant._resolve_target lowers the night's target to the plan
  (never below the floor, never on a balance or trip night).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import home_pattern as HP
from . import home_schedule as HS
from . import overnight_plan as OP
from . import solar_outlook as SO
from .const import (
    CA_CHARGE_POWER_KW,
    CA_SOLAR_BASELINE_KW,
    CA_SOLAR_EFFICIENCY,
    CA_SOLAR_ENTITY,
    CA_SOLAR_FLOOR_PCT,
    CA_SOLAR_FORECAST_ENTRIES,
    CA_SOLAR_HOME_SCHEDULE,
    CA_SOLAR_HOME_THRESHOLD,
    CA_SOLAR_MIN_HISTORY_DAYS,
    CA_SOLAR_NOTIFY,
    CA_SOLAR_OUTLOOK_ENTITY,
    CA_SOLAR_PLAN,
    CA_SOLAR_PRODUCTION_ENTITY,
    CA_SOLAR_SOURCE,
    DEFAULT_SOLAR_BASELINE_KW,
    DEFAULT_SOLAR_EFFICIENCY,
    DEFAULT_SOLAR_FLOOR_PCT,
    DEFAULT_SOLAR_HOME_THRESHOLD,
    DEFAULT_SOLAR_MIN_HISTORY_DAYS,
    SOLAR_PLAN_OFF,
    SOLAR_PLAN_ON,
    SOLAR_SOURCE_FORECAST,
    SOLAR_SOURCE_SENSOR,
)

_LOGGER = logging.getLogger(__name__)

_SEED_DAYS = 10            # what the recorder usually keeps
_RECOMPUTE = timedelta(minutes=10)
_NOTIFY_AT = (20, 0)       # evening note, local time
_PLAN_FRESH = timedelta(minutes=30)


def _f(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class SolarPlanner:
    """One per Charge Assistant. ``ca`` supplies the car's numbers:
    ``planner_inputs() -> dict(soc, target_pct, battery_kwh)`` (target without
    this planner's own adjustment), ``_connected_entity`` and
    ``_send_notification``."""

    def __init__(self, hass: HomeAssistant, entry_id: str, ca, get_opts: Callable[[], dict]):
        self.hass = hass
        self._ca = ca
        self._opts = get_opts
        self._store = Store(hass, 1, f"wallbox_gateway_solar_{entry_id}")
        self._home: dict = {}
        self._fc: dict = {}            # {"YYYY-MM-DD": {"forecast", "start", "actual"}}
        self._max_hour_kwh: float | None = None
        self._last_hour_reading: float | None = None
        self._unsubs: list = []
        self.result: dict | None = None       # the latest plan, for the sensor
        self._computed_at: datetime | None = None
        self._plan: OP.Plan | None = None
        self._input_unsub: Callable | None = None   # waiting for the SOC to load

    # ---- settings ---------------------------------------------------------
    def mode(self) -> str:
        return str(self._opts().get(CA_SOLAR_PLAN) or SOLAR_PLAN_OFF)

    def _o(self, key: str, default: float) -> float:
        return _f(self._opts().get(key), default)

    def _production_entity(self) -> str | None:
        o = self._opts()
        return o.get(CA_SOLAR_PRODUCTION_ENTITY) or o.get(CA_SOLAR_ENTITY) or None

    # ---- lifecycle --------------------------------------------------------
    async def async_start(self) -> None:
        if self.mode() == SOLAR_PLAN_OFF:
            return
        data = await self._store.async_load() or {}
        self._home = data.get("home") or {}
        self._fc = data.get("fc") or {}
        self._max_hour_kwh = data.get("max_hour_kwh")
        HP.prune(self._home, dt_util.now().date())
        if HP.history_days(self._home) < 7:
            await self._seed_from_recorder()
        self._unsubs.append(async_track_time_change(
            self.hass, self._on_half_hour, minute=[0, 30], second=20))
        self._unsubs.append(async_track_time_change(
            self.hass, self._on_notify_time, hour=_NOTIFY_AT[0], minute=_NOTIFY_AT[1], second=0))
        self._unsubs.append(async_track_time_interval(
            self.hass, self._on_recompute, _RECOMPUTE))
        self.hass.async_create_task(self.async_compute())

    async def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        self._stop_input_watch()
        await self._save()

    async def _save(self) -> None:
        try:
            await self._store.async_save({"home": self._home, "fc": self._fc,
                                          "max_hour_kwh": self._max_hour_kwh})
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Solar planner: save failed: %s", err)

    # ---- the home pattern -------------------------------------------------
    async def _seed_from_recorder(self) -> None:
        """Fill the pattern from the recorder's car-connected history."""
        eid = getattr(self._ca, "_connected_entity", None)
        if not eid:
            return
        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:
            return
        end = dt_util.utcnow()
        start = end - timedelta(days=_SEED_DAYS)
        try:
            states = await get_instance(self.hass).async_add_executor_job(
                history.state_changes_during_period, self.hass, start, end, eid)
        except Exception as err:  # noqa: BLE001 — no recorder, no seed
            _LOGGER.debug("Solar planner: recorder seed skipped: %s", err)
            return
        changes = []
        for st in (states or {}).get(eid, []):
            on = {"on": True, "off": False}.get(st.state)
            changes.append((dt_util.as_local(st.last_changed), on))
        n = HP.seed_from_intervals(self._home, changes,
                                   dt_util.as_local(start), dt_util.as_local(end))
        _LOGGER.info("Solar planner: seeded %d half hours of car-connected history", n)
        await self._save()

    @callback
    def _on_half_hour(self, now: datetime) -> None:
        local = dt_util.as_local(now)
        eid = getattr(self._ca, "_connected_entity", None)
        st = self.hass.states.get(eid) if eid else None
        if st is not None and st.state in ("on", "off"):
            HP.record(self._home, local, st.state == "on")
        if local.minute == 0:
            self._learn_production(local)
        HP.prune(self._home, local.date())
        self.hass.async_create_task(self._save())

    # ---- forecast correction ------------------------------------------------
    def _production_reading(self) -> float | None:
        eid = self._production_entity()
        st = self.hass.states.get(eid) if eid else None
        if st is None:
            return None
        v = _f(st.state, -1)
        if v < 0:
            return None
        unit = str(st.attributes.get("unit_of_measurement") or "").lower()
        return v / 1000.0 if unit == "wh" else v

    def _learn_production(self, local: datetime) -> None:
        """On the hour: track the most the panels made in an hour, and each
        day's actual production next to its forecast."""
        r = self._production_reading()
        if r is None:
            return
        if self._last_hour_reading is not None and r >= self._last_hour_reading:
            delta = r - self._last_hour_reading
            if delta < 50:   # ignore a jump (sensor reset or swap)
                self._max_hour_kwh = max(self._max_hour_kwh or 0.0, delta)
        self._last_hour_reading = r
        day = self._fc.setdefault(local.date().isoformat(), {})
        if "start" not in day:
            day["start"] = r
        # A daily sensor resets at midnight; a total keeps climbing.
        day["actual"] = r - day["start"] if r >= day["start"] else r
        for k in sorted(self._fc)[:-SO.FORECAST_KEEP_DAYS]:
            del self._fc[k]

    # ---- the forecast ---------------------------------------------------------
    async def _forecast_production(self) -> dict | None:
        """Hourly production from the forecast integrations, local hours."""
        try:
            from homeassistant.components.energy.websocket_api import (
                async_get_energy_platforms,
            )
            platforms = await async_get_energy_platforms(self.hass)
        except Exception as err:  # noqa: BLE001 — HA internals moved: no source
            _LOGGER.debug("Solar planner: energy forecast hook unavailable: %s", err)
            return None
        chosen = self._opts().get(CA_SOLAR_FORECAST_ENTRIES) or []
        replies = []
        for entry in self.hass.config_entries.async_entries():
            if entry.domain not in platforms:
                continue
            if chosen and entry.entry_id not in chosen:
                continue
            try:
                r = await platforms[entry.domain](self.hass, entry.entry_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Solar planner: %s forecast failed: %s", entry.domain, err)
                continue
            if r:
                replies.append(r)
        if not replies:
            return None
        return {dt_util.as_local(h): v for h, v in SO.from_wh_hours(replies).items()}

    def _sensor_outlook(self) -> dict | None:
        eid = self._opts().get(CA_SOLAR_OUTLOOK_ENTITY)
        st = self.hass.states.get(eid) if eid else None
        if st is None:
            return None
        out = SO.from_hourly_attr(st.attributes.get("hourly"))
        return {dt_util.as_local(h): v for h, v in out.items()} or None

    async def _spare_by_hour(self) -> tuple[dict, str, float | None]:
        """(spare kWh per local hour, source used, correction factor)."""
        src = str(self._opts().get(CA_SOLAR_SOURCE) or "auto")
        if src in ("auto", SOLAR_SOURCE_SENSOR):
            sens = self._sensor_outlook()
            if sens:
                return sens, "sensor", None
            if src == SOLAR_SOURCE_SENSOR:
                return {}, "sensor (no hourly outlook)", None
        if src in ("auto", SOLAR_SOURCE_FORECAST):
            prod = await self._forecast_production()
            if prod:
                today = dt_util.now().date().isoformat()
                day = self._fc.setdefault(today, {})
                day["forecast"] = round(sum(SO.for_day(prod, dt_util.now().date()).values()), 2)
                factor = SO.learn_factor(self._fc)
                baseline = self._o(CA_SOLAR_BASELINE_KW, DEFAULT_SOLAR_BASELINE_KW)
                return SO.spare(prod, factor, baseline, self._max_hour_kwh), "forecast", factor
        return {}, "none", None

    # ---- the plan -------------------------------------------------------------
    def _solar_day(self, now: datetime):
        """The daytime the night charge is planned against: tomorrow's from
        noon, today's after midnight."""
        return now.date() + timedelta(days=1) if now.hour >= 12 else now.date()

    def _schedule(self) -> tuple[list | None, str | None]:
        """The manual home schedule: (parsed, None), (None, None) when unset,
        or (None, error) when it can't be read (then the learned pattern is used)."""
        text = str(self._opts().get(CA_SOLAR_HOME_SCHEDULE) or "").strip()
        if not text:
            return None, None
        try:
            sched = HS.parse(text)
        except HS.ScheduleError as err:
            return None, str(err)
        return (sched or None), None

    def _home_chance(self, today, min_days: int, sched: list | None = None):
        if sched:
            return lambda hour: HS.hour_chance(sched, hour)

        def chance(hour: datetime):
            a = HP.probability(self._home, hour, today, min_days)
            b = HP.probability(self._home, hour + timedelta(minutes=30), today, min_days)
            if a is None or b is None:
                return None
            return (a + b) / 2
        return chance

    async def async_compute(self) -> None:
        if self.mode() == SOLAR_PLAN_OFF:
            self.result, self._plan = None, None
            return
        try:
            inputs = self._ca.planner_inputs()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Solar planner: inputs unavailable: %s", err)
            return
        if not inputs or inputs.get("soc") is None or not inputs.get("battery_kwh"):
            self.result = {"reason": "Needs the car's battery level and battery size."}
            self._plan = None
            self._watch_inputs()
            return
        self._stop_input_watch()
        now = dt_util.now()
        spare, source, factor = await self._spare_by_hour()
        day = self._solar_day(now)
        spare_day = SO.for_day(spare, day)
        min_days = int(self._o(CA_SOLAR_MIN_HISTORY_DAYS, DEFAULT_SOLAR_MIN_HISTORY_DAYS))
        sched, sched_err = self._schedule()
        p = OP.plan(
            soc=float(inputs["soc"]),
            target_pct=float(inputs["target_pct"]),
            floor_pct=self._o(CA_SOLAR_FLOOR_PCT, DEFAULT_SOLAR_FLOOR_PCT),
            battery_kwh=float(inputs["battery_kwh"]),
            efficiency=self._o(CA_SOLAR_EFFICIENCY, DEFAULT_SOLAR_EFFICIENCY) / 100.0,
            spare_by_hour=spare_day,
            home_chance=self._home_chance(now.date(), min_days, sched),
            threshold=self._o(CA_SOLAR_HOME_THRESHOLD, DEFAULT_SOLAR_HOME_THRESHOLD) / 100.0,
            charger_kw=self._o(CA_CHARGE_POWER_KW, 7.0),
        )
        self._plan, self._computed_at = p, now
        hist = HP.history_days(self._home)
        reason = p.reason
        if sched:
            reason += " Using your home schedule."
        elif hist < min_days:
            reason += f" Still learning when the car is home ({hist} of {min_days} days)."
        if sched_err:
            reason += f" Home schedule ignored ({sched_err})."
        self.result = {
            "tonight_kwh": p.tonight_kwh,
            "target_pct": p.target_pct,
            "need_kwh": p.need_kwh,
            "floor_kwh": p.floor_kwh,
            "solar_kwh": p.solar_kwh,
            "solar_hours": [f"{h.strftime('%H:%M')} {v:g} kWh" for h, v in p.solar_hours],
            "solar_day": day.isoformat(),
            "forecast_kwh": round(sum(spare_day.values()), 1),
            "source": source,
            "correction_factor": round(factor, 2) if factor is not None else None,
            "history_days": hist,
            "home_source": "schedule" if sched else "learned",
            "mode": self.mode(),
            "reason": reason,
        }

    # ---- re-plan as soon as the inputs load ------------------------------------
    def _watch_inputs(self) -> None:
        """After a restart the car's SOC sensor can load after the planner's
        first run; re-plan the moment it gets a value instead of waiting for
        the next 10-minute run."""
        if self._input_unsub is not None:
            return
        try:
            eids = [e for e in self._ca.planner_input_entities() if e]
        except Exception:  # noqa: BLE001
            eids = []
        if not eids:
            return

        @callback
        def _changed(event) -> None:
            new = event.data.get("new_state")
            if new is None or new.state in ("unknown", "unavailable", ""):
                return
            self._stop_input_watch()
            self.hass.async_create_task(self.async_compute())

        self._input_unsub = async_track_state_change_event(self.hass, eids, _changed)

    def _stop_input_watch(self) -> None:
        if self._input_unsub is not None:
            self._input_unsub()
            self._input_unsub = None

    @callback
    def _on_recompute(self, _now) -> None:
        self.hass.async_create_task(self.async_compute())

    @callback
    def _on_notify_time(self, _now) -> None:
        if not self._opts().get(CA_SOLAR_NOTIFY, True):
            return
        self.hass.async_create_task(self._notify())

    async def _notify(self) -> None:
        await self.async_compute()
        if not self.result or "tonight_kwh" not in self.result:
            return
        msg = self.result["reason"]
        if self.mode() != SOLAR_PLAN_ON:
            msg = "Preview, nothing changed: " + msg
        await self._ca._send_notification("plan", message_override=msg)

    # ---- the hook for _resolve_target -------------------------------------------
    def night_target(self, base: float) -> float:
        """The target to charge to tonight: the plan's when this is "on" and the
        plan is fresh, otherwise ``base``. Never above ``base`` and never below
        the floor."""
        if self.mode() != SOLAR_PLAN_ON or self._plan is None or self._computed_at is None:
            return base
        if dt_util.now() - self._computed_at > _PLAN_FRESH:
            return base
        floor = min(base, self._o(CA_SOLAR_FLOOR_PCT, DEFAULT_SOLAR_FLOOR_PCT))
        return max(floor, min(base, self._plan.target_pct))
