# ha-easytouch

Home Assistant HACS integration for **Micro-Air EasyTouch** RV thermostats.

Connects directly over Bluetooth Low Energy — no cloud, no MQTT bridge, no internet required.

> **Disclaimer:** This is an independent community integration and is not affiliated with, endorsed by, or supported by Micro-Air or any of its affiliates. Use it at your own risk.

## Features

- **Auto-discovery** via BLE advertisements (name prefix `EasyTouch*`)
- **Multi-zone support**: up to 4 zones, discovered automatically
- **Heat-source preset modes**: Heat Pump, Furnace, Heat Strip, Electric Heat, Gas Heat — only sources the device actually supports are shown
- **Dynamic fan modes**: fan options change automatically based on current HVAC mode (furnace/gas modes lock to `auto`)
- **4-second polling** matching the official EasyTouch app for fast UI response
- **500 ms debounce** on temperature and fan changes to avoid command flooding
- **Status suppression** (4 seconds after commands) to prevent UI bounce-back
- **Capability discovery**: reads MAV bitmask and FA array from device to show only valid modes and fan speeds
- **Diagnostics**: connection health sensors, device info (serial, firmware, model), one-click diagnostics download

## Entities

### Per zone

| Entity | Type | Notes |
|--------|------|-------|
| Climate | Climate | HVAC mode, fan mode, preset mode, temperatures |

### Device-wide (diagnostic)

| Entity | Type | Notes |
|--------|------|-------|
| Connected | Binary Sensor | BLE connection state |
| Authenticated | Binary Sensor | Protocol auth state |
| Data Healthy | Binary Sensor | Recent data received |
| Serial Number | Sensor (diag) | Thermostat serial |
| Firmware Version | Sensor (diag) | Firmware revision |
| Model Number | Sensor (diag) | Derived model (first 3 chars of serial) |
| Device Type | Sensor (diag) | TT field from status |
| Config Index | Sensor (diag) | CI field from status |
| Reboot | Button (config) | Reboot thermostat firmware |

## Requirements

- Home Assistant 2024.1+ with Bluetooth integration
- Bluetooth adapter on the HA host (or ESPHome BT proxy)
- Micro-Air EasyTouch thermostat within BLE range

## Installation (HACS)

1. Add this repository as a custom HACS repository
2. Install "Micro-Air EasyTouch"
3. Restart Home Assistant
4. The thermostat should auto-discover — or add manually via Settings → Devices & Services → Add Integration → Micro-Air EasyTouch

## Configuration

| Field | Description |
|-------|-------------|
| Bluetooth MAC | Auto-populated from BLE advertisement |
| Password | Leave blank if the thermostat has no password set |

## Protocol

- **Service**: `000000FF-0000-1000-8000-00805F9B34FB`
- **Password auth** (`DD01`): write raw UTF-8 password bytes; GATT success = authenticated
- **JSON write** (`EE01`): send commands (`Get Config`, `Get Status`, `Change`)
- **JSON read/notify** (`FF01`): receive responses; multi-packet JSON buffered automatically
- No BLE pairing / bonding required

### Key JSON structures

**Get Config** (discover zone capabilities):
```json
{"Type": "Get Config", "Zone": 0}
```

**Get Status** (poll every 4 seconds):
```json
{"Type": "Get Status", "Zone": 0, "EM": "x", "TM": 0}
```

**Change** (mode, temperature, fan):
```json
{"Type": "Change", "Changes": {"zone": 0, "power": 1, "mode": 5}}
{"Type": "Change", "Changes": {"zone": 0, "cool_sp": 74}}
{"Type": "Change", "Changes": {"zone": 0, "coolFan": 128}}
```

## HVAC mode / preset mapping

| HA HVAC Mode | Preset Mode | Device Mode |
|-------------|-------------|-------------|
| `heat` | Heat Pump | 5 |
| `heat` | Furnace | 4 |
| `heat` | Heat Strip | 7 |
| `heat` | Electric Heat | 12 |
| `heat` | Gas Heat | 13 |
| `heat` | Heat | 3 |
| `cool` | — | 2 |
| `auto` | — | 8 |
| `fan_only` | — | 1 |
| `dry` | — | 6 |
| `off` | — | 0 |

## Debug Logging

```yaml
logger:
  logs:
    custom_components.easytouch: debug
```

Use **Download diagnostics** from the integration page for a full runtime state dump.

## License

MIT
