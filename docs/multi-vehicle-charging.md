# Multi-Vehicle Charging — design spec

Status: **proposed** (not built). Extends the commute / projected-SOC work to more
than one car sharing a single wallbox.

## The core constraint

A wallbox charges **one car at a time** — it has one cable. So "multi-vehicle"
on a single charger is **sequential, not parallel**: the system can't move cars,
only the human can plug/unplug. That makes this fundamentally an **advisory
scheduler** — it decides *which car should be on the cable, and to what level,
when* — plus the existing autonomous charging for whatever is currently plugged.

Objective for "best outcome":
> Every car has **enough for its next trip by its departure**, while **minimising
> grid cost** (charge in cheap windows) and **maximising solar** — given one cable
> and human-driven swaps.

## Data model — per-car profile

Today's single mapping (SOC entity, capacity, departure, reserve, commute source)
becomes a **list of car profiles**. Each profile:

| field | example | source |
|---|---|---|
| `name` | "BYD Sealion 7" | user |
| `soc_entity` | `sensor.byd_..._battery_level` | car integration |
| `battery_kwh` | 80 | user |
| `odometer_entity` / `efficiency` | `sensor.byd_..._odometer` / 21 | car integration |
| `departure` | 07:30 | user (per-car, optional) |
| `reserve_pct` / `target_pct` | 20 / 80 | user |
| `present_entity` | `binary_sensor.byd_at_home` / cable | car or charger |

The learned daily-use / commute-target / **projected-SOC** all already compute
per-profile from these — projected-SOC is the key signal the scheduler ranks on.

## Identity — "which car is on the cable?"

**There is no vehicle ID to read.** Standard AC charging (IEC 61851 Control-Pilot,
what the Pulsar uses) is anonymous — no VIN/MAC is exchanged. ISO 15118 Plug &
Charge *could* carry a unique ID but the Pulsar over our BLE/BAPI path doesn't
expose it (and the basic Pulsar doesn't support PnC). Wallbox's RFID identifies
the *user card*, not the *car*. The charger gives us a **plug-in event**
(`car_connected` → true) and energy flow, nothing more — so identity must be
supplied.

**Primary: confirm-on-plug notification (with a smart pre-filled guess).**
1. `car_connected` flips true → *a* car plugged in.
2. **Guess** the car: whichever mapped car's **SOC is rising** once charging
   starts (most reliable), else "the car that's home", else most-urgent.
3. Fire an **actionable notification**: *"🔌 Plugged in the BYD? [Yes] · [No — the
   Tesla]"* — one tap confirms/corrects. If the SOC-rise guess holds for N minutes
   with no reply, **auto-confirm** it (usually zero taps).
4. The confirmed car becomes the **active car** for that plug-in session; all
   per-car target / commute / projection logic applies to it. It stays active
   until unplugged (`car_connected` → false), then re-asks on the next plug-in.

Better than a hardware ID: always works, any car/charger, and the SOC-rise
cross-check self-corrects.

**Fallbacks / upgrades:**
- **Manual `input_select.charging_now`** — always-available override; also the
  answer target of the notification.
- **Per-car plug/charging sensor** — if exactly one car integration reports
  "charging"/"plugged in", use it directly (no notification needed).
- **Single car configured** → skip identity entirely (it's that car).

### Safe fallback when identity *can't* be determined

Resolution order, each step a fallback for the one above:
1. **Single car** → it's that car. Done.
2. **Confident auto-signal** — lone plug sensor, or SOC clearly rising on exactly
   one car → use it.
3. **Sticky last car** — assume the **last-confirmed** car (people usually charge
   the same car) → use it, but still send the confirm notification so a wrong
   guess is one tap to fix. (`_active_car()` already does this: CA_ACTIVE_CAR
   else first profile.)
4. **Genuinely unknown** (never confirmed, ambiguous signal, no reply) → the
   `unknown_car` policy, configurable, safest first:
   - **`ask` (default)** — do **not** autonomously start a car-specific charge;
     send the "which car?" notification and wait. Native schedule + manual
     charging still work, so the car isn't stranded — the assistant just doesn't
     *guess* a target.
   - **`conservative`** — charge to the **lowest** commute target / reserve across
     all cars, so an unknown car is never *over*-charged (under-charge is
     recoverable; a one-tap confirm raises it). Good for "always have some range".
   - **`assume_last`** — trust the sticky car and act on its target.

Safety invariant: when unsure the assistant only ever does the **reversible,
conservative** thing (charge a little, or nothing) — never a high/forced charge
on a guess. Worst case is a slightly-low battery the user tops up after
confirming, never an over-charge or a stranded car.

## Scheduling protocol (best outcome)

Single cable + fixed deadlines = a classic **Earliest-Deadline-First (EDF)**
sequencing problem, made cost-aware. Because we can't actuate swaps, the output
is a **ranked plug-in plan** + autonomous charging of the current car.

Each evaluation tick (or on plug/unplug / nightly):

1. **Per car, compute** (kWh and time):
   - `deficit_kwh = max(0, (target_pct − soc_pct)/100 × battery_kwh)`
   - `need_kwh = max(0, (need_pct − soc_pct)/100 × battery_kwh)` where
     `need_pct = max(reserve, commute_target)` — the *must-have*, vs `target` the
     *nice-to-have*.
   - `charge_h = need_kwh / charge_power_kw`
   - `deadline = next departure` ; `slack_h = (deadline − now) − charge_h`
   - **urgency** = negative slack first, then `days_until_reserve` (the
     projected-SOC attribute) — a car that "won't make it" outranks one that will.

2. **Feasibility** across all cars on one cable: sum of `charge_h` for must-haves
   must fit the cheap windows before the earliest deadline. If it doesn't, flag
   *"can't fully charge both before 7:30 — prioritising BYD"* and cover must-haves
   in EDF order, dropping nice-to-haves.

3. **Recommendation** (the "plug in X" nudge): the highest-urgency car **not
   currently on the cable** that still needs its must-have →
   *"🔌 Plug in the BYD tonight — projected 18% (below reserve). The Tesla is fine
   at 55%."* Fires when the cable is free or the current car has reached its
   must-have and another car is more urgent (*"BYD's at 60% — swap to the Tesla to
   reach its 7am trip"*).

4. **Autonomous charging** of the plugged car: the existing single-car engine,
   parameterised by **that car's** profile (target, commute target, cheap window,
   departure). Unchanged logic, per-profile config.

This is greedy/EDF — provably optimal for meeting deadlines on one machine when
all jobs are known; cost-aware slotting layers the cheap-window planner on top.

## Surfaces

- **Sensors per car**: `daily_use_average`, `commute_charge_target`,
  `projected_soc`, **`charge_urgency`** (rank/score), plus one gateway-level
  **`recommended_plug_in`** (text: the car to plug in next + why).
- **Add-on GUI**: Charge Assistant gains a **car list** (add/edit/remove
  profiles) above the mode config; the mode config applies to "the plugged-in
  car". A small "Plug-in plan" panel shows the ranked order.
- **Lovelace**: extend the commute card into a per-car repeater + a headline
  "Plug in next: **BYD**" banner.
- **Notification**: the plug-in recommendation as an actionable notify.

## Phasing

- **P1 — profiles + identity:** car-profile list, manual `charging_now` selector,
  per-profile sensors. Charging logic uses the selected car's profile. (No
  recommendation yet — just correct per-car config + advice sensors.)
- **P2 — recommendation engine:** urgency ranking + `recommended_plug_in` sensor +
  actionable notify. Auto-identity (SOC-rises / plug sensor) as an upgrade.
- **P3 — feasibility + cost-aware plan:** EDF feasibility check across cars,
  cheap-window slotting, "can't do both" warnings, the Lovelace plan panel.

## Open questions

- Per-car *departure* vs one shared window — likely per-car, with the window as a
  global cheap-price gate.
- How aggressively to nudge swaps mid-charge (anti-spam, like the solar nudge).
- Two physical chargers (future) → parallel, drops the single-cable constraint and
  becomes simple per-charger single-car.
