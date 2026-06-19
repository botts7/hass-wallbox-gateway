# Wallbox Gateway — Charge Assistant blueprint

One blueprint, one automation, a **Mode** dropdown — so charge strategies can't
conflict. Import `charge_assistant.yaml` and create a single automation.

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
