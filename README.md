<p align="center">
  <img src="custom_components/wallbox_gateway/brand/logo.png" alt="Wallbox Gateway" width="600">
</p>

# Wallbox BLE Gateway — Home Assistant Integration

Native Home Assistant integration for the
[ESP32 Wallbox BLE Gateway](https://github.com/botts7/esp32-wallbox).
Polls the gateway's local HTTP API and exposes the charger as native
HA entities — **no MQTT broker required**.

## Why this exists

The gateway already publishes MQTT discovery topics, so anyone running
[Mosquitto](https://www.home-assistant.io/integrations/mqtt/) gets HA
entities automatically. That's fine for most setups.

This integration is for users who:

- Run HA Container, HA Core, or a venv install (no Add-on store,
  often no broker)
- Want a **config-flow wizard** instead of YAML
- Want **native services** to script schedules and current limits
  from the same scripts that already control other HA devices
- Want **statistics → HA Energy dashboard** with proper device classes
- Want to keep one fewer broker process in the loop

## Roadmap

| Version | Adds |
|---------|------|
| v0.1.0    | Config flow + DataUpdateCoordinator + 6 sensors + 2 binary_sensors |
| **v0.2.0** | 3 switches (charging, lock, auto_lock) + Number (max_current) + Select (eco_smart_mode) + Button (refresh_now); coordinator polls `r_dca` for working `mains_voltage` + `house_power` |
| v0.3.0    | Number (auto_lock_minutes, eco_smart_power_pct) + Button (reboot_gateway, once firmware-side auth-only reboot endpoint lands) + Update entity (firmware version surface) |
| v0.4.0    | Services: `add_schedule`, `delete_schedule`, `toggle_schedule`, `set_max_current` |
| v0.5.0    | Long-term statistics for HA Energy dashboard |
| v1.0.0    | Stable / submit to HA core |

## Installation

### Via HACS (when published)

1. HACS → Integrations → ⋮ → Custom repositories
2. Paste this repo URL, choose category **Integration**
3. Install **Wallbox BLE Gateway**, restart HA
4. Settings → Devices & Services → Add Integration → search "Wallbox"

### Manual

Copy `custom_components/wallbox_gateway/` into your HA config directory's
`custom_components/` folder, restart HA, then add the integration from
Settings → Devices & Services.

## Configuration

When adding the integration you'll be asked for:

- **Gateway host** — local IP or mDNS name (e.g. `192.168.1.42` or
  `wallbox-gw.local`)
- **Username** — defaults to `admin`. Leave blank if web auth is off.
- **Password** — the gateway's web-auth password if set; same value
  as the OTA password (shown on the serial log at boot).
- **Poll interval** — seconds between gateway pulls. Default 10.

The integration probes `/api/health` + `/api/status` to confirm
connectivity and pulls the charger serial number as the device's
stable unique-id.

## Entities

| Entity | Source | Device class |
|--------|--------|--------------|
| `binary_sensor.<name>_ble_connected`  | `/api/status -> ble` | connectivity |
| `binary_sensor.<name>_charging`       | charger_status == 1 | battery_charging |
| `sensor.<name>_charger_status`        | enum, 19 values | enum |
| `sensor.<name>_charging_power`        | kW | power |
| `sensor.<name>_session_energy`        | kWh (measurement, resets) | energy |
| `sensor.<name>_house_power`           | W (if MID meter installed) | power |
| `sensor.<name>_mains_voltage`         | V (L1) | voltage |
| `sensor.<name>_next_scheduled_charge` | next enabled schedule (timestamp) | timestamp |
| `binary_sensor.<name>_plug_reminder`  | a charge is due soon and the car isn't plugged in | — |
| `sensor.<name>_last_burst_energy`     | kWh of the most recent recorded charge burst (fw v3.2+) | energy |
| `sensor.<name>_charge_log_count`      | recorded charge bursts (diagnostic, fw v3.2+) | — |
| `sensor.<name>_ble_rssi`              | dBm (disabled by default — diagnostic) | signal_strength |
| `switch.<name>_charging`              | start / stop charging | switch |
| `switch.<name>_lock`                  | lock / unlock charger | switch |
| `switch.<name>_auto_lock`             | auto-lock-after-disconnect (60 s default window) | switch |
| `number.<name>_max_current`           | 6 – 32 A slider | current |
| `select.<name>_eco_smart_mode`        | Disabled / Full Green / Eco Smart | — |
| `button.<name>_refresh_now`           | force the coordinator to poll immediately | update |

## Compatibility

- Home Assistant **2024.12** or newer
- Gateway firmware **v3.0.0** or newer (`/api/health` + the diagnostic
  endpoints landed in 3.0). The `last_burst_energy` / `charge_log_count`
  sensors need **v3.2.0+** (the charge-interval capture).

## License

MIT. See [LICENSE](LICENSE).
