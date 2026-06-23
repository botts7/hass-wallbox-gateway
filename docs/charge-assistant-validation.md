# Charge Assistant — On-Charger Validation Checklist

Run this on a real charger to validate the Charge Assistant control loop and the
cost sensors. The pure logic (planner, guards, cost engine, charger adapter) is
covered by `tests/run_all.py` (48 cases); this checklist covers what only real
hardware + a running Home Assistant can prove.

**Prereqs**
- Integration **0.15.0+** and Add-on **0.22.0+** installed on a HAOS / Supervised box.
- Tested against a **Pulsar MAX** (set-current / start-stop / owner tagging
  confirmed). On the **original Zentri Pulsar**, dynamic current is disabled by
  design (see capability gating).

**Safety** — Do this while you can supervise. You can abort anytime by setting
the gateway's **Charge Control Owner** back to *Wallbox native schedule*, or by
stopping charging manually. The assistant only acts when it owns control and
stands down for ~30 min after any manual/other command.

---

## 0. Plumbing (no charging)
- [ ] Add-on → **🤖 Assistant** page loads; mode tiles, live entity values, and the summary render.
- [ ] Pick a mode, **Save** → toast "Saved"; reload the page → settings persisted.
- [ ] (Optional) Developer Tools → States: the `wallbox_gateway` config-entry options reflect the saved config.

## 1. Reminder mode (safe — only notifies)
- [ ] Enable a trigger (e.g. nightly), set the notify service, **Save**.
- [ ] Call service `wallbox_gateway.test_reminder` → notification arrives on your phone.

## 2. Hand control to the integration
- [ ] On the **gateway's own Settings** page, set **Charge Control Owner = Home Assistant Integration**. (This pauses the charger's native schedule — expected.)
- [ ] Assistant page: the owner warning clears; the header pill shows the active mode.

## 3. Smart charge → target (car plugged in)
- [ ] Set target **below** current SOC, enable **auto-start** → charging **starts**.
- [ ] Let it reach the target → charging **stops** at the cap.
- [ ] **Manual-charge respect:** start a charge yourself from the app, then let SOC pass the target → the assistant must **not** stop your manual charge.

## 4. Departure just-in-time
- [ ] Set a departure ~15 min out + battery capacity + charge power → it **delays** the start, then starts in time to reach target by departure.

## 5. Cheapest-window (needs a price entity with a forecast — Amber / Nord Pool / Tibber)
- [ ] Enable cheapest + pick the price entity (GUI shows ✓ "forecast detected") → charging lands in the **cheapest** hours.
- [ ] **Floor:** set a tight departure that cheap hours can't cover → it charges anyway to be ready in time.

## 6. Price cap
- [ ] Set a **low** cap (below the current price) → won't start. Raise it above the price → starts.
- [ ] Confirm the departure floor still overrides the cap when time is short.

## 7. Solar (Pulsar MAX supports dynamic current)
- [ ] Surplus source = your sensor (or **grid** / **solar − load**) → start/stop tracks surplus, respecting the hold time.
- [ ] **Dynamic current:** watch `max_charging_current` track the surplus — **allow ~5–8 s lag** (gateway realtime cache; this lag is expected and is why the controller reads back with a delay).
- [ ] **House-load limit:** set a limit → charge current trims to keep total house draw under it.

## 8. Battery care
- [ ] Set a trip target of 100% until a near time → the effective target rises to 100, then **reverts** to the everyday target after that time passes.

## 9. Cost sensors
- [ ] Set a tariff in the Add-on (Sessions → Tariff) and Save → the integration entities **Charging cost (7 days)** / **(this month)** populate within a poll cycle; check that Statistics graphs them.

## 10. Arbitration + cleanup
- [ ] Issue a manual start/stop → the assistant **stands down ~30 min** (no fighting).
- [ ] Restore **Charge Control Owner** to your preference when finished.

---

**If something misbehaves:** Settings → System → Logs (filter `charge_assistant`),
capture the line plus the Assistant config. If a cost/savings figure looks wrong,
use the Add-on savings card's **Export feedback** (anonymised, on-device — nothing
is sent automatically).
