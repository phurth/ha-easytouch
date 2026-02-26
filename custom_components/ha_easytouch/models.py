"""Data models for the EasyTouch integration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ZoneConfig:
    """Configuration for a single thermostat zone, from Get Config response."""

    zone: int
    available_modes_mask: int   # MAV bitmask: bit N set → device mode N is supported
    fan_array: list[int]        # FA array, 16 entries (one per mode 0-15)
    min_cool_sp: int
    max_cool_sp: int
    min_heat_sp: int
    max_heat_sp: int


@dataclass
class ZoneState:
    """Live status for a single thermostat zone, from Z_sts response."""

    zone: int
    ambient_temp: int           # Current room temperature (°F)
    cool_sp: int                # Cool-mode setpoint
    heat_sp: int                # Heat-mode setpoint
    auto_cool_sp: int           # Auto-mode cool setpoint (high)
    auto_heat_sp: int           # Auto-mode heat setpoint (low)
    dry_sp: int                 # Dry-mode setpoint
    mode_num: int               # Current device mode (0-13)
    fan_only_speed: int         # Fan speed when in fan-only mode
    cool_fan_speed: int         # Fan speed when cooling
    electric_fan_speed: int     # Fan speed for heat-pump / heat-strip modes
    auto_fan_speed: int         # Fan speed in auto mode
    gas_fan_speed: int          # Fan speed for furnace / gas-heat modes
    cycle_active: bool          # STATUS_FLAGS bit 0
    is_cooling: bool            # STATUS_FLAGS bit 1
    is_heating: bool            # STATUS_FLAGS bit 2
    fault: int = 0              # Fault code (index 14)


@dataclass
class ThermostatState:
    """Aggregate thermostat state across all zones."""

    available_zones: list[int]
    zones: dict[int, ZoneState]
    system_power: bool = True
    # Device info parsed from status / BLE Device Information Service
    serial_number: str | None = None
    firmware_version: str | None = None
    device_type: str | None = None
    config_index: str | None = None
    model_number: str | None = None

    @property
    def primary_zone(self) -> ZoneState | None:
        return self.zones.get(0)
