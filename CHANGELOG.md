# Changelog

All notable changes to the Wallbox BLE Gateway HA integration.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
