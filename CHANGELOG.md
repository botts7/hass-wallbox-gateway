# Changelog

All notable changes to the Wallbox BLE Gateway HA integration.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.15.0] - 2026-06-22

### Added
- **Config bridge services** `wallbox_gateway.get_config` and
  `wallbox_gateway.set_config`. `get_config` returns the entry's current
  options (Charge Assistant + tunables) as service response data;
  `set_config` shallow-merges an options object into the entry and reloads
  (restarting the Charge Assistant with the new config). These let the
  companion Add-on host a rich Charge Assistant configuration GUI without the
  native options-flow wizard. Both match the entry by gateway `host` (or the
  only entry if omitted). The options flow remains a fully-functional
  fallback writing the same `entry.options`.
- **Dynamic current control** in Solar mode — the assistant can now command
  the charge current (not just start/stop), modulating it to follow solar
  surplus within configurable min/max amps (supply voltage + phases convert
  power to current). An optional **house-load limit** trims charge current so
  total house draw stays under a cap, read from a user-chosen grid-power
  entity (works without the charger's Power Boost meter) or the charger's own
  meter. New options: `min_current_a`, `max_current_a`, `solar_dynamic`,
  `supply_voltage`, `supply_phases`, `load_limit_w`, `load_power_entity`.
- **Cheapest-window charging** (sub-option of Smart-charge) — charge only during
  the cheapest forecast hours that still reach target by departure. Reads a
  price entity's forecast (Nord Pool `raw_today`/`raw_tomorrow`, Amber
  `forecasts`, Tibber, generic). Safety nets: the departure just-in-time floor
  forces charging if cheap hours run short (car always ready in time), and it
  only ever *stops* a charge it started itself — never a manual one. Falls back
  to plain just-in-time when the price entity has no forecast. New pure,
  unit-tested planner (`price_planner.py`); new options `cheapest_window`,
  `price_entity`.
- **Battery care + cost cap** (smart-charge). A daily target is your everyday
  ceiling; an optional **trip target** raises it only until a deadline
  (`trip_until`) then auto-reverts by time — no one-shot state. A **price cap**
  (`price_cap`) is a hard ceiling that never charges above a price (your
  departure floor still overrides so the car is ready in time). Pure,
  unit-tested guards (`charge_guards.py`); new options `trip_target_pct`,
  `trip_until`, `price_cap`.
- **Charger-control adapter** (`charger_control.py`) — all charge commands now
  go through a `ChargerControl` interface (Wallbox adapter today), so other
  chargers can be added without touching the modes/planner/GUI.
- **Native options flow parity** — the dynamic-current, cheapest-window and
  battery-care/price-cap settings are now in the native options flow too, so
  Container/Core users (no Add-on) can configure them.
- **Surplus source** for solar mode — works without a ready-made "surplus"
  sensor: derive it from a **grid-power** sensor (export = surplus; configurable
  sign) or from **solar production − house load**. New options `surplus_source`,
  `grid_entity`, `grid_export_negative`, `solar_entity`, `load_entity` (pure,
  unit-tested derivation in `charge_guards.py`).
- **Charging cost sensors** — `Charging cost (7 days)` and `(this month)`,
  computed natively from the firmware charge-log + your tariff (each burst
  billed at the rate of the hours it ran in; solar is free). Real HA entities
  with long-term statistics. The tariff is mirrored from the Add-on into the
  config entry (`entry.options['tariff']`) via the existing config bridge —
  set it once in the Add-on's tariff editor. New pure cost engine
  (`cost_engine.py`), a Python port of the Add-on's `cost.js` proven equivalent
  by shared-scenario tests.
- **Unit-test suite** (`tests/`, 48 cases) — pure-logic tests for the planner,
  charger adapter, and guards, plus controller-decision (glue) tests with a
  fake hass (effective target, price-cap gating, trip target, surplus
  derivation). Run with `py tests/run_all.py`.

## [0.14.4] - 2026-06-22

### Fixed
- **Grid power L1/L2/L3** are now enabled by default (diagnostic category),
  matching the MQTT discovery entities — they were created but disabled, so
  they showed in MQTT but not in the integration. Reported by a Pulsar Max +
  EM340 user.

## [0.14.3] - 2026-06-22

### Added
- Per-phase grid power **Grid power L1 / L2 / L3** sensors (from the EM340 /
  3-phase Power Boost `r_dca` reading). Diagnostic, off by default — enable on
  a 3-phase install. The summed **House power** + **Lifetime energy** sensors
  were already present.

## [0.14.2] - 2026-06-21

### Fixed
- Original/Zentri Pulsar (#12): the **Charger status**, **Charging**, and
  **Car connected** entities now read `r_dat.st` when the charger doesn't serve
  `r_sta` (so they work on the original Pulsar, not just Plus/MAX), via a new
  charger-family-aware status helper. Status labels use a Zentri-specific map
  (st4 = charge ramp, no longer shown as "Paused"). Charging power already
  flowed through the firmware's derived `cp` — needs gateway firmware
  **v3.2.0-beta.2+**.

## [0.3.1] - 2026-06-12

### Added

- **Live-session energy sensors**, backed by the charger's `r_lse`
  feed (polled alongside the other BAPI reads, same best-effort
  fallback):
  - `sensor.green_energy_session` — solar kWh for the current session
  - `sensor.grid_energy_session` — grid kWh for the current session
  - `sensor.surplus_power` — live solar surplus (kW)
  - `sensor.active_feature` — which feature is controlling (diagnostic,
    disabled by default)
  - `sensor.control_mode` — canonical control-mode code (diagnostic,
    disabled by default)

### Security

- `r_lse` includes a `user_id` field (the Wallbox account id). It is
  parsed out and **never** exposed as an entity, attribute, or log
  line — `_parse_lse` reads only the public energy/feature fields.

## [0.3.0] - 2026-06-10

### Added

- **Full parity with the firmware's MQTT discovery** — ~30 additional
  sensors and binary_sensors so the native integration surfaces the
  same data an MQTT user already gets (charger firmware/project,
  session counters, power-boost limit, network info, OCPP status,
  notifications, power-sharing, phase-switch, timezone, boot/health
  diagnostics). Diagnostic entities are disabled by default via
  `entity_category`.
- **Controls:** auto-lock timeout (number, minutes), Eco-Smart solar
  target (number, %), reboot-charger button.

## [0.2.0] - 2026-06-08

The control-surface release. v0.1 was sensors-only and didn't let HA
automations actually do anything; v0.2 adds the entities that map
directly onto what the gateway already exposes — `start`/`stop`/`lock`/
`unlock`/`current` actions plus the `s_alo` and `s_ecos` BAPI methods.

### Added

- **`switch.charging`** — start / stop charging via
  `/api/command?action=start|stop`.
- **`switch.lock`** — lock / unlock via `action=lock|unlock`. Reads
  state from the realtime charger status (code 6 = Locked).
- **`switch.auto_lock_enabled`** — toggles auto-lock-after-disconnect
  via the `s_alo` BAPI bare-integer shape. Restores the previously-set
  window when re-enabling; defaults to 60 s on first turn-on. Read
  state is parsed from `g_alo`, which the coordinator pulls each tick.
- **`number.max_current`** — 6 – 32 A slider that hits
  `action=current&value=N`. Reads from the realtime `cm` field with
  status `ic` as a fallback.
- **`select.eco_smart_mode`** — Disabled / Full Green / Eco Smart
  options via `s_ecos`. Preserves the existing `esp` (solar power
  target %) across mode changes so toggling Disabled ↔ Eco doesn't
  reset the user's solar target.
- **`button.refresh_now`** — forces a coordinator refresh without
  waiting for the next poll tick. Useful after writing settings via
  the dashboard or curl when HA state hasn't caught up yet.
- Coordinator now pulls `g_alo`, `g_ecos`, and `r_dca` each tick
  (best-effort, via `return_exceptions=True` so a transient BLE
  blip doesn't flap the device offline). Prior parsed values
  carry forward when a BAPI read fails.
- **`binary_sensor.schedule_paused`** — surfaces the Wallbox app's
  "Schedule & Solar charging paused" state. Backed by
  `r_dat.gen != 0`, the sticky manual-override flag: ON when the
  schedule has been overridden (Stop in our gateway, or Pause in
  the app), OFF when armed. Independent of whether the charger is
  currently charging — a manual Start while the schedule is paused
  will keep this sensor ON, matching the official app's behavior.
- **`button.resume_schedule`** — fires the gateway's
  `/api/command?action=resume`, which maps to `s_cmode` with
  `{"mode":0}`. Clears the override flag so the schedule + Eco
  Smart loops resume controlling the charger. HA automations
  paired with `binary_sensor.schedule_paused` can implement
  "auto-resume after N minutes of manual override" patterns.

### Fixed

- `sensor.<name>_mains_voltage` + `sensor.<name>_house_power` were
  reading from `chg_volt` / `chg_house_power` keys in `/api/status`
  which the gateway doesn't populate. Both values actually live
  behind the BAPI `r_dca` (power-meter) call. Coordinator now
  polls `r_dca` alongside the existing endpoints, parses
  `{v1, p1, p2, p3}` into a `meter` dict, and both sensors read
  voltage_v + house_power_w from there. Same path the gateway's
  own dashboard uses. House power is summed across all three
  phases (negative = exporting to grid, positive = importing).

### Deferred to v0.3

- **`button.reboot_gateway`** — `POST /api/reboot` requires a CSRF
  token paired with the browser session, which the integration can't
  obtain without a firmware-side auth-only endpoint. 3.0's frozen
  firmware branch can't add that without re-opening the freeze, so
  this lands in v0.3 after the next firmware cycle exposes an
  integration-friendly reboot path.
- Granular `number.auto_lock_minutes` for the auto-lock window
  (currently fixed at the prior seconds value or 60 s default).
- Granular `number.eco_smart_power_pct` for the solar power target
  (currently preserved but not exposed for editing).

### Compatibility

- Home Assistant **2024.12** or newer.
- Gateway firmware **v3.0.0** or newer.

## [0.1.0] - 2026-06-07

First release. Read-only sensor surface — install the integration
to see the charger as a native HA device without needing an MQTT
broker.

### Added

- Config flow wizard (single-step probe; charger serial number used
  as the stable unique id).
- `DataUpdateCoordinator` polling four endpoints in parallel each
  tick (`/api/status`, `/api/charger`, `/api/diag/disconnects`,
  `/api/health`) with a 10 s default interval.
- Six sensor entities: `charger_status` (enum), `charging_power`
  (kW), `session_energy` (kWh), `house_power` (W), `mains_voltage`
  (V), `ble_rssi` (dBm, disabled by default).
- Two binary sensors: `ble_connected`, `charging`.
- Single device per gateway with manufacturer/model/firmware fields
  populated from the gateway status response.
- English translations.
