# Wallbox Gateway — Charge Assistant blueprint

> ⚠️ **BETA — not yet road-tested.** The notify modes (Reminder / Prompt) are
> low-risk. The **Scheduled (HA-driven)** mode *actually starts and stops your
> charge* — try it on a few cycles and keep an eye on it before trusting it
> unattended. **Please report results** (works / doesn't, your charger + car) at
> <https://github.com/botts7/hass-wallbox-gateway/issues> — feedback shapes the
> stable release and the upcoming guided setup.

One blueprint, one automation, a **Mode** dropdown — so charge strategies can't
conflict. Import `charge_assistant.yaml` and create a single automation.

## Decide what you want (walk-through)

Answer these and you'll land on the right Mode + fields.

**1. Should Home Assistant actually start/stop the charge — or just message you?**
- *Just message me* → go to Q2.
- *HA should control charging* → **Scheduled charge (HA-driven)**. Go to Q3.

**2. (Messaging) Do you set charging times on the charger itself (Wallbox app schedule)?**
- *Yes, I use the charger's schedule* → **Reminder** mode — nudges you if a
  scheduled charge is coming up but the car isn't plugged in.
  *Need:* Plug-in reminder sensor + Notify device. *Optional:* Charging switch
  ("Start now" button), Battery sensor (skip if already charged), Next-charge
  sensor (shows the time).
- *No, I just plug in whenever* → **Plugged-in prompt** mode — when you plug in
  with nothing scheduled, it asks "start now?".
  *Need:* Car connected + Next-charge sensor + Notify device. *Optional:* Charging
  switch, Battery sensor.

**3. (HA-driven) When, and to what level?**
- *At a set time, to a fixed %* → set **Start time** + **Target %**.
- *To a level based on how much I drive* → also add the **Rolling-distance
  sensor** (a Statistics helper on your odometer) + **Range on a full charge**.
- *Only top up when the battery is actually low ("override")* → just set a
  **Target %**: it starts at the time **only if SOC is below target**, and stops
  when it reaches it — so a near-full car won't charge, a low one will. This is
  how you override/skip a charge based on SOC.
  *Always need:* Car connected + Charging switch + Battery sensor + Start time.

**Conditions you can layer on any mode:** quiet hours · skip/don't-charge if
battery already ≥ X% · stale-SOC guard (ignore an old reading) · hard stop time
(Scheduled safety cut-off).

## Pick a Mode

| Mode | Use if… | Does |
|------|---------|------|
| **Reminder (charger schedule)** | you charge on the **charger's own schedule** | Notifies you if a charge is due soon but the car isn't plugged in. |
| **Scheduled charge (HA-driven)** | you want **HA** to run charging (no charger schedule) | Starts at your time once plugged in, stops at a target battery %. |
| **Plugged-in prompt (ad-hoc)** | you have **no schedule** | When you plug in with nothing scheduled, asks if you want to start. |

> HA can't hide fields by mode, so every field shows — each is tagged `[Reminder]`,
> `[Scheduled]`, `[Prompt]`, `[shared]`, or `[core]`. Fill the two `[core]` sensors
> plus only the fields your mode uses.

## Entities to pick (Wallbox Gateway device)

Use **one** source — the HA **Integration** *or* **MQTT discovery**, not both
(having both gives you two of every entity, which is the main way people pick the
wrong one). Names below are the Integration's; MQTT names are equivalent.

| Field | Entity |
|-------|--------|
| `[core]` Plug-in reminder sensor | `binary_sensor.…_plug_in_reminder` |
| `[core]` Car connected sensor | `binary_sensor.…_car_connected` (or your car's plug sensor) |
| `[shared]` Charging switch | `switch.…_charging` — **ON = start, OFF = stop** (also powers the "Start now" button + HA-driven charging) |
| `[shared]` Battery level sensor | your car's SOC sensor (any integration) |
| `[Reminder/Prompt]` Next-charge sensor | `sensor.…_next_scheduled_charge` |
| `[Prompt]` Charging sensor | `binary_sensor.…_charging` |

## Dynamic, travel-based target (Scheduled mode)

Make the target track how much you actually drive — works with **any** car:

1. **Create a Statistics helper** (once): Settings → Devices & Services →
   **Helpers** → **Create Helper** → **Statistics** → your car's **odometer**,
   characteristic **change**, max age **7 days**. It now reads km driven in the
   last 7 days.
2. In the blueprint: **Rolling-distance sensor** → that helper, **Days covered** →
   7, **Range on a full charge (km)** → your car's full range, **Travel buffer %**
   → e.g. 15.

Target = `avg_daily_km / full_range_km + buffer`, capped at 100%. Want
day/week/month sensitivity? Make 1- / 7- / 30-day helpers and point at whichever.
You can also point **Dynamic target source** at any `input_number` or template
sensor instead — the blueprint just reads it. Priority: dynamic source → travel →
fixed.

## Notes

- Actionable buttons / tap-to-open need the HA **companion app**. The "Start now"
  button only appears if you set the **Charging switch**.
- The charge control is the **Charging switch** — there's no separate "start"
  entity. The Start-now button and HA-driven Scheduled mode both flip this switch.
- Charger-agnostic: triggers on entities, so it also works with other charger
  integrations that expose equivalents.
