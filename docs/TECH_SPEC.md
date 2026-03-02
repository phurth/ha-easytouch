# Micro-Air EasyTouch HACS Integration — Technical Specification

## 1. Purpose and Scope

`ha_easytouch` provides direct BLE integration for Micro-Air EasyTouch thermostats in Home Assistant, with zone climate control and diagnostics.

This document describes the HA-native implementation and excludes mobile bridge specifics.

## 2. Integration Snapshot

- **Domain:** `ha_easytouch`
- **Primary runtime component:** `EasyTouchCoordinator`
- **Platforms:** `binary_sensor`, `button`, `climate`, `sensor`
- **Transport:** BLE GATT
- **Coordinator mode:** internal poll loop (`update_interval=None`)

## 3. Configuration and Entry Setup

- config flow discovers thermostat by BLE advertisement
- setup stores address/password config and creates coordinator
- startup connection is backgrounded to avoid blocking HA bootstrap

## 4. Runtime Lifecycle

1. Entry setup forwards platforms and starts coordinator.
2. Coordinator establishes persistent BLE session.
3. Authentication runs on connection.
4. Zone configuration discovery populates capability models.
5. Poll loop drains command queue, then requests status.
6. Disconnect path triggers reconnect loop with backoff.

## 5. Protocol and Transport Model

### 5.1 Communication pattern

EasyTouch command handling is read-after-write:

1. write JSON command
2. wait short delay
3. read response characteristic
4. reassemble fragmented JSON payload

GATT layout:

- **Service:** `000000ff-0000-1000-8000-00805f9b34fb`
- **Password/auth characteristic:** `0000dd01-0000-1000-8000-00805f9b34fb`
- **JSON command write:** `0000ee01-0000-1000-8000-00805f9b34fb`
- **JSON response read:** `0000ff01-0000-1000-8000-00805f9b34fb`

Device metadata is additionally read from standard Device Information service (`0x180A`) characteristics (manufacturer/model/serial/firmware/hardware revision).

### 5.2 Capability discovery

- mode availability from `MAV` bitmask
- fan behavior by `FA` matrix
- setpoint bounds from zone config payload

Coordinator semantics align to device JSON operations:

- `Get Config` for zone capability discovery
- `Get Status` for periodic runtime updates
- `Change` for mode/setpoint/fan writes

Polling and timing constants:

- status poll interval: `4.0s`
- config zone delay: `0.3s`
- auth delay: `0.2s`
- post-write read delay: `0.1s`
- status suppress window after writes: `4.0s`

## 6. State and Entity Model

- `ZoneConfig` stores per-zone capabilities and limits.
- `ZoneState` stores live mode/setpoint/fan/status values.
- `ThermostatState` aggregates zones and device metadata.
- climate entities map HA-facing controls to model state.
- diagnostic entities expose connection/auth/health/device info.

## 7. Command and Control Surface

- HVAC mode, preset, setpoint, and fan writes route via queued `Change` commands.
- mode-specific field mapping prevents invalid fan/setpoint writes.
- debounce and temporary status suppression reduce bounce after writes.

Mode/fan mapping details:

- Device mode constants span `0..13` (off/fan/cool/heat/furnace/heat-pump/dry/auto variants).
- Gas/furnace-related heat modes constrain fan handling.
- Fan payload values use explicit numeric mapping (`off=0`, `low=1`, `high=2`, `auto=128`).
- JSON fan field is selected by active mode (`eleFan`, `gasFan`, `coolFan`, `autoFan`, `fanOnly`).

## 8. Reliability and Recovery

- persistent BLE session model
- reconnect backoff with cap
- startup retry for slot/contention scenarios
- health status tied to recent successful data flow

Operational thresholds:

- reconnect base/cap: `5s` / `120s`
- stale timeout: `300s`
- command debounce delay: `0.5s`

## 9. Diagnostics and Observability

- binary sensors: connected, authenticated, data healthy
- diagnostic sensors: serial, firmware, model, config index
- diagnostics export includes coordinator state and latest parsed data

## 10. Security and Safety Notes

- local BLE-only control path (no cloud dependency in integration)
- command serialization via queue avoids overlapping writes
- mode-aware command translation reduces unsafe invalid command combinations

## 11. Evolution Notes (Commit History)

Recent trajectory includes:

- move to persistent connection architecture
- reconnect/session robustness improvements
- protocol sequencing/timing refinement
- optimistic state and suppression logic tuning
- migration to `ha_easytouch` domain naming

## 12. Known Constraints

- poll-based behavior means delayed reads directly affect freshness
- exposed capabilities depend on correct device config payloads
- BLE adapter/proxy transport quality affects latency and reliability

## 13. Extension Guidelines

1. Keep queue-first command ordering before status polling.
2. Preserve capability-derived exposure (avoid hardcoded universal modes).
3. Extend typed models before adding platform/entity behavior.
4. Add suppression/debounce considerations for new writable fields.
5. Keep reconnect-safe state transitions when adding session features.
