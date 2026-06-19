# Wallbox Gateway — charge automation blueprints

Three blueprints turn the gateway's data into hands-off charging. They're
**alternative strategies — pick the one that matches your setup**, don't run
more than one at once (they'd fight or double-notify).

| Blueprint | Use this if… | What it does |
|-----------|--------------|--------------|
| **Plug-in reminder** | you charge on the **charger's own schedule** | Reminds you (phone) if a charge is due soon but the car isn't plugged in. |
| **Scheduled charge to target** | you want **Home Assistant** to run charging (no charger schedule) | Starts at your time once plugged in, stops at a target battery %. |
| **Plugged in, no schedule prompt** | you charge **ad-hoc** (no schedule at all) | When you plug in with nothing scheduled, asks if you want to start. |

## What the gateway gives you

Whether you use the **HA Integration** or plain **MQTT discovery**, the gateway
exposes these (names are prefixed with your device, e.g. `wallbox_pulsar_max`):

- `binary_sensor.…_plug_in_reminder` — ON when a charge is due within your lead
  time and the car isn't plugged in (set the lead minutes on the gateway's
  `/config` page).
- `sensor.…_next_scheduled_charge` — timestamp of the next charger schedule
  (empty if there's no enabled schedule).
- `binary_sensor.…_car_connected` — ON when the car is plugged in.
- `switch.…_charging` — turn ON to start charging, OFF to stop.

> The reminder/next-charge are computed on the gateway, so every surface
> (dashboard, MQTT, Integration) shows the same values.

## Install the blueprints

Settings → Automations & Scenes → **Blueprints** → **Import Blueprint**, and
paste the raw URL of each YAML in this folder (or copy the files into
`config/blueprints/automation/wallbox_gateway/` and *Reload Automations*).

---

## 1. Plug-in reminder

For chargers using their **native schedule**. Create an automation from it and set:

- **Plug-in reminder sensor** → `binary_sensor.…_plug_in_reminder`
- **Notify device** → your phone (Home Assistant app)
- *(optional)* **Charging switch** → `switch.…_charging` — adds a **"Start
  charging now"** button to the notification
- *(optional)* **Open this page when tapped** → a Lovelace path, e.g.
  `/lovelace/wallbox`
- *(optional)* **Battery level sensor** + **Skip when battery at or above %** —
  don't nag if the car's already charged enough
- *(optional)* **Ignore battery level if older than (minutes)** — if your SOC
  sensor is stale, the reminder is sent anyway rather than wrongly skipped
- *(optional)* **Next-charge sensor** → `sensor.…_next_scheduled_charge` — adds
  the scheduled time to the message
- *(optional)* **Quiet hours** — set start = end to disable

---

## 2. Scheduled charge to target (Home-Assistant-driven)

For charging **without** the charger's native schedule. HA starts charging at
your time and stops at a target battery %.

- **Charging switch** → `switch.…_charging`
- **Car connected sensor** → `binary_sensor.…_car_connected`
- **Battery level sensor** → your car's SOC sensor
- **Start time** → when to begin
- **Target battery level (%)** → fixed goal (e.g. 80), *or* set a dynamic one below
- *(optional)* **Hard stop time** → safety cut-off (leave `00:00:00` to stop only on target)

### Make the target dynamic (average daily travel)

So the goal tracks how much you actually drive — works with **any** car:

1. **Create a Statistics helper** (one-time): Settings → Devices & Services →
   **Helpers** → **Create Helper** → **Statistics**:
   - *Entity* → your car's **odometer** sensor
   - *Characteristic* → **change**
   - *Max age* → e.g. **7 days** (the window you want to average over)
   - This helper now reads "km driven in the last 7 days".
2. In the blueprint set:
   - **Rolling-distance sensor** → that Statistics helper
   - **Days covered** → match the window (7)
   - **Range on a full charge (km)** → your car's full-charge range
   - **Travel target buffer (%)** → headroom on top (e.g. 15)

The blueprint computes `target = avg_daily_km / full_range_km + buffer`, capped
at 100%. Want day/week/month sensitivity? Make 1-day / 7-day / 30-day Statistics
helpers and point the blueprint at whichever window you prefer (or swap it from
another automation). You can also point **"Dynamic target source"** at any
`input_number` or template sensor instead — the blueprint just reads it.

---

## 3. Plugged in, no schedule prompt

For **ad-hoc** charging. When you plug in and there's no schedule, it prompts you.

- **Car connected sensor** → `binary_sensor.…_car_connected`
- **Next-charge sensor** → `sensor.…_next_scheduled_charge` (empty = no schedule → prompt)
- **Notify device** → your phone
- *(optional)* **Charging switch** → `switch.…_charging` for the "Start now" button
- *(optional)* **Charging sensor** → suppress the prompt while already charging
- *(optional)* battery skip / quiet hours, as above

---

## Notes

- **Actionable buttons / tap-to-open** need the Home Assistant **companion app**
  (iOS/Android). The "Start charging now" button only appears if you set the
  Charging switch.
- **Don't enable more than one** of these for the same charger — they're
  different strategies. (e.g. the no-schedule prompt would fire for a planner
  user who deliberately has no charger schedule.)
- All three are **charger-agnostic**: they trigger on entities, so they also
  work with other charger integrations that expose equivalents.
