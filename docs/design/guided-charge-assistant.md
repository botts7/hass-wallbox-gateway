# Design spec — Guided Charge Assistant (Integration-native)

Status: **proposed** · Target: a post-v0.4.0 flagship release of the
`wallbox_gateway` HA Integration.

## 1. Problem & goal

The Charge Assistant *blueprint* (`blueprints/automation/wallbox_gateway/
charge_assistant.yaml`) works, but HA blueprints can't hide fields by mode,
can't validate selections, and can't build helpers for the user. New users
face ~24 fields and have to hand-create Statistics/Template helpers.

**Goal:** an in-Integration **guided setup** that takes the guesswork out —
the user answers a short wizard and the Integration *runs the charge logic
itself* (no hand-built automation, no helpers). Install → answer ~4 questions
→ it just works.

Non-goal: generating a user-editable HA automation. HA has no supported API
for an integration to create one; instead the Integration **is** the
assistant (runs the logic in Python). The blueprint stays as the transparent
/ power-user / no-Integration option — the two are complementary.

## 2. Architecture

- **Config = Options flow** on the existing config entry (Settings → the
  Wallbox Gateway integration → **Configure**). Multi-step + conditional +
  validated — the dynamic UX blueprints can't do. Stored in `entry.options`.
- **Runtime = a `ChargeAssistant` controller** created per entry in
  `__init__.py:async_setup_entry`, reading `entry.options`. It subscribes to
  state changes + time events and drives behaviour. Reloads on options update
  (`entry.add_update_listener`).
- **No user helpers.** Travel averaging is done internally by a RestoreSensor
  that tracks daily odometer deltas. Target is computed in Python.
- **Reuses** the existing `coordinator.py` data (gateway entities already
  known), `entity.py` base, and the gateway entities the blueprint uses
  (`plug_in_reminder`, `car_connected`, `charging` switch,
  `next_scheduled_charge`).

```
config_flow.py (OptionsFlow)  ──writes──▶  entry.options
                                              │
__init__.py async_setup_entry ──creates──▶  ChargeAssistant(controller)
                                              │ subscribes
   state_change(connected/soc/plug_reminder) + time(start/stop) + 5-min tick
                                              │ acts
   notify service  /  switch.turn_on|off  /  exposes native sensors
```

## 3. Config (Options) flow — the wizard

Mirrors the blueprint's "decide what you want" walk-through. Each step
validates (entity exists, right domain) before `async_create_entry`.

1. **`async_step_init` — Mode**
   `select`: *Off* / *Reminder* / *Scheduled (HA-driven)* / *Plugged-in prompt*.
   Routes to the matching step. *Off* clears the assistant.

2. **`async_step_reminder`** (Mode = Reminder)
   - notify target (required) — `select` of `notify.*` services / mobile devices
   - charge switch (optional → enables "Start now")
   - battery sensor (optional → skip-if-charged), skip-% , stale-minutes
   - quiet hours start/end
   - Defaults pre-filled from the integration's own entities (it knows its
     `plug_in_reminder` / `next_scheduled_charge` — no need to ask).

3. **`async_step_scheduled`** (Mode = Scheduled)
   - charge switch (required — validated)
   - car-connected sensor (default = integration's own `car_connected`)
   - battery sensor (required for target-stop; if omitted, only hard-stop)
   - start time (required), hard stop time (optional)
   - **target sub-step** `select`: *Fixed %* / *From daily travel* / *External entity*
     - Fixed % → number
     - From daily travel → odometer entity + full-range km + buffer + window
       days (Integration builds the rolling average itself — no Statistics
       helper)
     - External entity → pick an `input_number`/sensor
   - quiet hours / stale-minutes

4. **`async_step_prompt`** (Mode = Plugged-in prompt)
   - notify target (required), car-connected sensor (default own)
   - charge switch (optional → "Start now"), charging sensor (optional suppress)
   - prompt delay, battery skip-%, quiet hours

Validation examples: required entity missing → `errors["base"]="entity_required"`;
chosen switch not a `switch.` → rejected; Scheduled with travel-target but no
odometer → reject. **This is the conflict/won't-work prevention the user asked
for** — impossible in a blueprint, native here.

## 4. Stored options schema (`entry.options["charge_assistant"]`)

```jsonc
{
  "mode": "scheduled",            // off | reminder | scheduled | prompt
  "notify_target": "notify.mobile_app_dans_s23",
  "charge_switch": "switch.wallbox_pulsar_max_charging",
  "connected_entity": "binary_sensor.wallbox_pulsar_max_car_connected",
  "soc_entity": "sensor.byd_sealion_7_battery_level",
  "soc_max_age_min": 60,
  "skip_above_pct": 80,
  "quiet": {"start": "00:00:00", "end": "00:00:00"},
  "start_time": "23:00:00",
  "stop_time": null,
  "target": {"kind": "travel",   // fixed | travel | entity
             "fixed_pct": 80,
             "entity": null,
             "odometer": "sensor.byd_sealion_7_odometer",
             "full_range_km": 450, "window_days": 7, "buffer_pct": 15},
  "tap_path": "", "message": "...", "title": "..."
}
```

## 5. Runtime controller (`charge_assistant.py`)

Port the blueprint's `choose` branches to Python. Helpers (pure functions,
reused/tested): `resolve_target()`, `in_window(now, start, stop)`,
`soc_fresh(state, max_age)`, `quiet_now(start, end)`, `connected()`.

- **Reminder:** `async_track_state_change_event(plug_in_reminder → 'on')` →
  if `soc_skip_ok` and `quiet_ok` → `notify` (actionable). Listen for the
  `mobile_app_notification_action` event (`WB_CA_START`) → `switch.turn_on`.
- **Scheduled:** triggers = `async_track_time_change(start)`,
  state(connected→on), state(soc), `async_track_time_interval(5 min)`,
  time(stop). On fire: if connected & in_window & below target → turn_on; if
  fresh soc ≥ target → turn_off; at stop → turn_off. Idempotent.
- **Prompt:** state(connected→on, debounced) → if next_charge empty & not
  charging & soc_skip_ok & quiet_ok → notify (actionable). Same start button.

Single mode per entry → strategies can't conflict (the merge rationale,
enforced structurally).

## 6. Native entities created

- `sensor.<dev>_charge_target` — resolved target % (fixed / travel / entity),
  `device_class: battery`. Always present in Scheduled mode.
- `sensor.<dev>_avg_daily_distance` — the internally-tracked rolling average
  (km), so travel-target users can see/verify it. (Replaces the Statistics
  helper.)
- `binary_sensor.<dev>_charge_assistant_active` — diagnostic: is the assistant
  currently driving a charge.
- (Optional) `select.<dev>_charge_mode` — change mode without the options flow.

## 7. Travel tracking (replaces the Statistics helper)

`AvgDailyDistanceSensor(RestoreSensor)`: at local midnight, record
`odometer_now`; keep a ring of the last `window_days` daily deltas; state =
mean. Survives restart (RestoreEntity). Handles odometer resets/unavailable
(ignore non-monotonic / unknown samples). No external helper required.

## 8. Notifications

- Actionable via the chosen `notify` target: `data.actions` ("Start charging
  now" → `WB_CA_START`), `data.clickAction` (tap path). Quiet hours + stale-SOC
  guard applied before sending (reuse the blueprint logic).
- One controller per entry → one notification author → no duplicates even if
  MQTT + Integration both installed (the dedup principle).

## 9. Coexistence & migration

- **Blueprint stays** for no-Integration / transparent users. Docs explain:
  Integration wizard = turnkey; blueprint = manual/customizable. Don't run both
  for the same charger (controller checks for a same-target blueprint
  automation and raises a **Repairs** issue if detected — best-effort).
- **MQTT vs Integration:** wizard defaults to the Integration's own entities;
  warns if duplicate MQTT entities are selected.
- Optional **Repairs** issue if Scheduled mode is on while the charger's native
  schedule is also active (the "two things charging" conflict).

## 10. Testing

- Unit: `resolve_target`/`in_window`/`soc_fresh`/`quiet_now` truth tables;
  RestoreSensor delta/rollover/midnight logic.
- Config-flow tests: each step, validation errors, conditional routing.
- Integration tests (pytest-homeassistant-custom-component): mode behaviours
  with mocked state changes + time travel (`async_fire_time_changed`).
- Manual on the real HAOS box: each mode end-to-end against the live gateway.

## 11. Open decisions

- Options flow on the existing entry vs a dedicated "Charge Assistant" config
  entry? (Lean: options flow — one device, simpler.)
- Expose `select.charge_mode` for quick mode switching, or options-only?
- Ship the rolling-average as an always-on sensor, or only when travel-target
  is chosen?
- HACS-default / core-submission implications of an integration that sends
  notifications + controls a switch (acceptable, but document intent).

## 12. Phasing

1. Options flow (mode + reminder) + controller(reminder) + tests.
2. Scheduled mode + `charge_target` + travel RestoreSensor.
3. Prompt mode + actionable buttons + Repairs conflict checks.
4. Docs + migration guide from the blueprint.
