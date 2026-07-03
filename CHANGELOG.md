# Changelog

All notable changes to the Wallbox BLE Gateway HA integration.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.19.0] - 2026-07-03

### Added
- **Halo LED control** (requested by _Mike). Two new entities, read from
  `g_halocfg` and written via `s_halocfg` (each write preserves the other
  fields):
  - **`switch.halo_standby`** — dim-the-ring-when-idle on/off.
  - **`number.halo_brightness`** — LED brightness 0–100 %.
  Verified end-to-end on real hardware (read + non-destructive write round-trip).

## [0.18.2] - 2026-07-03

### Fixed
- **Coordinator crash when the gateway returns an empty status cache.** On a
  fresh boot or a marginal BLE link the gateway can return `{"status": null,
  "realtime": null}`, and `.get("status", {})` returns `None` (not the default),
  so the nested `.get("r")` raised `AttributeError: 'NoneType' object has no
  attribute 'get'` — taking down the **entire** coordinator and making every
  entity unavailable (looked like "the integration won't connect"). Now guarded
  with `or {}`. (#20, reported by _Mike)

## [0.18.1] - 2026-07-03

### Added
- **Gateway-firmware compatibility warning.** The coordinator reads `gw_fw` from
  `/api/status` (firmware v3.2.0-beta.8+) and logs a one-time warning when the
  gateway firmware is older than **v3.0.0** — below which older firmware can
  leave entities blank. Closes the firmware ⇄ integration compatibility axis.

## [0.18.0] - 2026-07-02

First **stable** release of the Charge Assistant config bridge + multi-vehicle
work that shipped through the 0.18.0b1–b15 betas. Promoted to stable so it's
available on the HACS default channel (no pre-release toggle needed) — the
Add-on's Charge Assistant page needs the `get_config`/`set_config` services this
release exposes.

### Compatibility
- Pairs with the **Wallbox Gateway Add-on ≥ 0.40.0**. The Add-on will tell you to
  update if it finds this integration older than 0.18.0.

### Summary (since 0.14.x stable)
- **Charge Assistant config bridge:** `get_config` / `set_config` services so the
  Add-on GUI can read and write the integration's options (Charge Assistant +
  tunables) and reload.
- **Multi-vehicle charging** (advisory EDF scheduler): profiles, plugged-in
  identity, per-car targets, unknown-car policy.
- **Commute-based adaptive target** + projected-SOC, cost/charge-log sensors,
  and the composable Charge Assistant (window + strategy modules).

## [0.18.0b15] - 2026-06-28

### Fixed
- **Multi-car identity: SOC-rise auto-confirm now runs every coordinator tick**
  instead of a single 4-minute check. A slow / big-battery car (where SOC hadn't
  risen ≥1% by the 4-min mark) no longer gets stuck "uncertain" and capped at the
  conservative target for the whole session — it confirms as soon as its SOC rises.
- **Identity confidence resets to confident on unplug** (was left "uncertain", so
  a between-sessions manual charge on the sticky car could wrongly hit the cap).
- **Commute learner: stamp the 1-hour throttle even when a recorder read fails**,
  so a recorder error can't re-dispatch the history read every tick.

## [0.18.0b14] - 2026-06-28

### Added
- **`unknown_car` policy** — what to do while the plugged-in car is still a guess
  (before you confirm / SOC-rise settles): **conservative** (default) charges only
  to the lowest target across cars so an unknown car is never over-charged;
  **ask** holds off auto-start (target = current SOC) until confirmed; **assume**
  acts on the best guess immediately. Identity confidence resets on each plug-in
  and clears when you confirm (tap) or the SOC-rise auto-confirms. Selectable in
  the options flow (and the add-on Vehicles card).

## [0.18.0b13] - 2026-06-28

### Added
- **"Plug in car X" nudge.** When the cable is free and a car needs charge, an
  actionable notification names which car to plug in (gated on "home",
  anti-spammed: 4h cooldown, re-armed when the recommended car changes).
- **Cheap-window feasibility.** "Plug in next" now carries `feasible`,
  `needed_hours`, `available_hours` and a `feasibility_note` — e.g. *"~10h needed
  but only ~6h before departure — prioritising BYD"* — when the cars' must-haves
  can't all fit the cheap window before the earliest departure (one cable =
  charge times add up).
- `examples/lovelace-commute-card.yaml` now surfaces **On the cable** + **Plug in
  next** (with reason + feasibility warning) when ≥2 cars are configured.

## [0.18.0b12] - 2026-06-28

### Added
- **Multi-vehicle recommendation (P3) — "Plug in next" sensor.** Ranks the cars
  by urgency (lowest days-to-reserve / "won't make it" first, then biggest
  deficit) and names the most-urgent car that still needs charge and isn't
  already on the cable — i.e. which one to plug in next (or swap to). Attributes:
  a friendly `reason` + a `ranked` per-car table (soc / target / deficit /
  days-until-reserve). None when nothing needs charge or single-car.

## [0.18.0b11] - 2026-06-28

### Added
- **Multi-vehicle identity (P2a) — confirm-on-plug.** With ≥2 cars configured,
  a plug-in (`car_connected` rising edge) starts the identity flow: it guesses
  the car (a car whose **SOC is rising** = the one charging; else the most-urgent
  car by days-to-reserve / lowest SOC; else the sticky last car) and sends an
  actionable **"Which car is charging?"** notification — one tap confirms or
  corrects. If you don't reply, the **SOC-rise auto-confirms** after a few
  minutes. The chosen car becomes the active car and the per-car target /
  commute / projection all apply to it.
- **Active vehicle** sensor — which mapped car is on the cable now (single-car:
  unknown). The confirmed car is sticky for the session.

## [0.18.0b10] - 2026-06-28

### Added
- **Multi-vehicle foundation (P1).** The learner / commute target / projected-SOC
  now resolve against the *active* car (the one on the cable) via car profiles
  (`cars` list + `active_car`). Each profile carries its own
  soc_entity/battery_kwh/target/commute settings; anything unset falls back to
  the top-level keys, so **single-car configs are unchanged**. Per-car learner
  cache. (Config UI in add-on 0.37.0; identity/recommendation engine next.)

## [0.18.0b9] - 2026-06-28

### Added
- **Projected SOC after a day's driving** sensor — a forward-looking "will I make
  it?" insight: `current SOC − learned daily-use% × 1 day`, floored at 0. Shown
  even when commute mode is off. Attributes: `daily_use_pct`,
  `days_until_reserve` (how many days of driving until you hit the reserve floor
  with no charging), and `below_reserve_tomorrow`.

## [0.18.0b8] - 2026-06-28

### Added
- **Commute learner "learn from" source.** The adaptive target can now be based
  on what you actually drive, not just what the charger delivered:
  - **Charger energy** (default, unchanged) — energy the wallbox delivered/day;
    no car integration needed.
  - **Car odometer + efficiency** — reads a total-km sensor's recorder history,
    computes km/day, ×efficiency (kWh/100km, default 18). Distance-true and still
    counts driving when you charge elsewhere.
  - **Car battery-level (SOC)** — reads the SOC sensor's history and sums the
    daily drops (driving), ×battery capacity.
  History-backed sources are read off the recorder on a throttled 1-hour refresh
  and cached, so the target math stays synchronous; they degrade gracefully to
  the fixed target if the recorder/entity isn't available.

## [0.18.0b7] - 2026-06-28

### Added
- **Commute-based adaptive target.** Turn it on under the Target-charge mode and
  the assistant learns how much you actually drive — from the firmware's real
  charge-log (energy added per day ≈ energy driven) — and sizes the SOC target to
  it automatically: `reserve + avg_daily_use × cover_days + margin`, clamped to
  `[30%, your everyday target]`. An overnight cheap-window charge then tops up
  just enough for the commute (plus a margin) instead of always filling to a
  fixed number. Tunables (all optional): floor (`commute_reserve_pct`, default
  20), margin (`commute_margin_pct`, default 10), days-to-cover
  (`commute_cover_days`, default 1) and the learning window
  (`commute_window_days`, default 7).
- Two new sensors: **Daily use (average)** (kWh/day, learned) and **Commute
  charge target** (%, what it would charge to) — shown even when commute mode is
  off, as advice.

## [0.18.0b6] - 2026-06-28

### Added
- **Auto-resume Eco-Smart / native schedule after a manual charge** (on by
  default; opt-out in the add-on's Integration settings). A manual/owner start
  pauses Eco-Smart (`gen != 0`); when the charge then stops and the charger is
  left paused + idle, the integration clears the override (`action=resume`) so the
  charger's own Solar + schedule loops take back over — no need to tap "Resume
  schedule". Only runs when the integration isn't the active controller, debounced
  ~3 min and rate-limited so it never spams. New top-level option `auto_resume_eco`.

## [0.18.0b5] - 2026-06-27

### Fixed
- **Solar charging got stuck after a stop and wouldn't re-charge when surplus
  returned.** The Solar / Smart-Solar stops no longer "hand back" to the
  charger's native control (a `resume` that left it stuck). The assistant now
  keeps control and simply restarts on the next solar tick — so when surplus
  comes back, it charges again. (Target-mode finish still hands back, unchanged.)

### Changed
- **"Solar available" reminder won't spam.** Even as surplus flaps up and down
  (passing clouds), the nudge fires at most once every 4 hours. The
  notification's **Skip** button still dismisses it for the rest of the day, and
  **Snooze** holds it for an hour.

## [0.18.0b4] - 2026-06-27

### Added
- **"Solar available" plug-in reminder** — a new reminder trigger that nudges you
  to plug in when there's spare solar and the car is unplugged, so you don't waste
  free surplus. Uses your existing solar/surplus source; fires once on the rising
  edge (re-arms when surplus drops). Configurable threshold (`solar_remind_kw`).
- **"Only when home" condition** (`home_entity`) — an optional presence
  entity (person/device_tracker) that must be `home` for any reminder to fire.
  Pairs with the solar reminder ("plug in for free solar, but only when I'm home").

## [0.18.0b3] - 2026-06-27

### Added
- **"Solar can fill up to %" field** in the Smart + Solar options flow — the
  configurable ceiling for the new solar-past-target behaviour (`solar_max_soc`,
  default 100%). Lower it to protect the battery (e.g. 90%) while still letting
  free solar charge past the grid target.

## [0.18.0b2] - 2026-06-27

### Changed
- **Smart + Solar no longer caps free solar at the SOC target.** The SOC target
  now only stops *grid* top-up — when there's solar surplus the assistant keeps
  charging past the target (grabbing all the free energy), up to a solar ceiling
  (`solar_max_soc`, default 100%). Previously it stopped dead at 80% even with
  the sun pouring in, wasting surplus.

### Fixed
- **Hand back on stop.** When the Solar / Smart-Solar modes stop a charge (target
  reached, surplus gone), and when a target charge finishes, the assistant now
  clears any lingering Eco/schedule pause so the charger's own Solar + schedule
  control resumes — it no longer leaves the charger stuck "paused" and unable to
  charge from solar on its own.

## [0.18.0b1] - 2026-06-27

The cheap window now genuinely **bounds** a smart charge, and a forced start
holds against Eco-Smart. Prompted by a live "80% by 8am" charge that quietly did
nothing (departure just-in-time was waiting) and an earlier one that started then
got re-queued by the charger's Solar-Only Eco-Smart.

### Changed
- **The charging window now GOVERNS grid charging.** Previously, setting a
  departure time made the just-in-time logic *ignore* the window — so a
  "00:00–06:00, ready by 08:00" setup would charge ~04:54–07:50, two hours into
  peak. Now, when a window is enabled, the assistant charges *just-in-time to
  finish by the window END*, **stops at the window end**, and only pushes
  charging outside the cheap hours toward the departure deadline if you turn on
  **overrun** ("keep charging past the window") and/or **pre-start**. The window
  wins by default; the departure is the fallback, not the master.

### Added
- **Eco-Smart re-assert watchdog.** An owner-tagged start overrides the charger's
  Solar-Only / schedule pause (like a manual start in the official app), but some
  chargers re-queue it a beat later when there's no solar at night. The assistant
  now verifies the start actually held and re-asserts it (clearing the sticky
  Eco/schedule pause first) a few times before warning you — so a night grid
  charge sticks without you having to disable Eco-Smart.
- **`sensor.<name>_next_charge_start`** — a TIMESTAMP sensor showing *when* the
  assistant will next start charging, with `status` + `reason` attributes
  (charging now / at target / ready to start / when there's solar / off). So a
  just-in-time charge that's deliberately waiting reads as "starts ~03:54 to
  reach 80% by 06:00" instead of looking broken.

### Fixed
- **False "charger didn't accept Stop" alert at target.** The finish-verify
  checked too soon: after a Stop the charge power ramps down over several seconds
  and the coordinator only polls ~10s, so a normal stop still *looked* like
  charging and the verify warned even though it stopped. Now it requests a fresh
  reading, waits longer than a poll cycle (18s), and only treats a clearly-
  significant charge power (> 1 kW) as "ignored the Stop" — so a ramp-down tail /
  poll lag no longer false-alarms, while a charger that genuinely drops a Stop is
  still caught and retried.

## [0.17.0] - 2026-06-24

Forced grid charge with a clean hand-back to Solar/schedule — validated live on
a Pulsar Max.

### Added
- **`button.reboot_gateway`** — reboots the ESP32 gateway itself (the v0.2
  deferred item). Uses the firmware's new auth-only `POST /api/reboot_gateway`
  (no CSRF), so the stateless integration can call it; the CSRF-gated
  `/api/reboot` stays for the gateway's own web UI. Needs gateway firmware with
  the endpoint (v3.2.0-beta.8+).
- **Auto-start grace period** (`autostart_grace_min`, default 0 = off). When set,
  the assistant first sends *"Charging will start in N min — tap Not now / Start
  now"* and only begins after the countdown, so you can override. The countdown
  is cancelled automatically if the car unplugs or the target is reached.
- **"Charging now" follow-up** confirming the charge actually began (after the
  grace period, or immediately when grace is 0).
- **Clean hand-back on finish.** A forced charge (owner-tagged start) overrides
  the charger's Solar-Only / schedule pause for the session. When it reaches
  target the assistant **stops**, then hands control back to the charger's own
  Solar + schedule loops **only if the charger is actually still paused**
  (checks the `gen` flag first). A plain stop normally leaves it armed (`gen=0`),
  so no `resume` is sent — avoiding a blind `resume` that would *restart*
  charging. (We do **not** toggle Eco-Smart off/on — the start already overrides
  it, and toggling risked leaving Solar disabled.)
- **Stop is verified, not trusted (cross-charger hardening).** Some chargers
  (original/Zentri Pulsar, older Plus firmware) can silently drop a Stop. The
  finish now **reads back the charge state, retries the Stop**, and only declares
  "Charged to X%" + hands control back once charging has actually ceased — and
  warns you if the charger keeps ignoring Stop, instead of falsely reporting done.

### Fixed
- **"Paused" charger status was misleading.** Wallbox status 4 ("Paused")
  covers both an active override (Schedule/Solar paused, `gen≠0`) **and** a
  plain stopped/idle session (`gen=0`, e.g. after reaching target). The status
  sensor now shows **"Connected — not charging"** for the idle case and reserves
  **"Paused"** for a real override.
- **Auto-start deadband suppressed the initial charge.** "Plug in at 77%, target
  80%" wouldn't start because a 5% anti-flap margin gated *all* starts. The 5%
  deadband now only applies to **re-starts after reaching target** this session;
  a fresh plug-in starts on any real gap. Reset on unplug.
- **Stop-at-target could miss** when the charging binary sensor lagged 'off'
  after a start/reload — `_is_charging` now also checks the live charge power.
- **Auto-start could stall after an HA restart** when the car was already
  plugged in (no SOC/plug edge to react to) — added a steady tick for autostart
  plus a deferred startup re-check.
- **Notification action buttons were truncating** on the phone — shortened the
  titles ("Start now" / "Snooze 1h" / "Skip").

## [0.16.0] - 2026-06-24

Composable Charge Assistant + native-schedule import.

### Added
- **Composable Charge Assistant**: plug-in **reminders are now a layer** that
  runs on top of any charging strategy; a new **Smart + Solar** strategy
  (solar-first, grid only to finish by departure / inside the window); and an
  **allowed charging window** (e.g. 00:00–06:00) with pre-start, overrun, and a
  cost-warning when a charge runs outside it. Legacy `mode: reminder` configs
  migrate automatically.
- **`import_native_schedules`** service — mirrors the charger's native
  schedules into HA (persisted snapshot) so they're never lost while the
  integration controls charging. Returns the decoded schedules.

### Changed / Fixed
- Options flow: the reminder's **"Tap opens" path** is now a dropdown of your
  Lovelace dashboards + views (still accepts free-text), matching the Add-on.
- Options flow: the **charging window + auto-start grace** no longer appear in
  the **Solar** step — they only gate *grid* charging (Smart charge / Smart +
  Solar), and pure Solar charges from surplus anytime, so they don't apply there.
- `set_config` now allow-lists the option keys it accepts, and the options flow
  **merges** into `entry.options` instead of replacing it (preserves
  `poll_interval` / `tariff`).
- Fixed a `NameError` that crashed solar mode on start.
- **Reminders now say what the *assistant* will do**, not the charger's stale
  native-schedule time. A plug-in reminder under Smart charge + autostart now
  reads "…will charge to 80% as soon as you plug in" (or "…in the 00:00–06:00
  window"), Solar reads "…will charge from spare solar", etc. Reminder-only /
  Off still shows the charger's native next-charge time (correct there, since
  the assistant isn't acting). Previously every reminder appended the native
  next-charge time even when the assistant was going to start immediately —
  promising the wrong thing.

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
