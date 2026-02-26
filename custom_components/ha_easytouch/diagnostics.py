"""Diagnostics support for EasyTouch."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import EasyTouchCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: EasyTouchCoordinator = hass.data[DOMAIN][entry.entry_id]

    zone_configs = {}
    for zone, cfg in coordinator.zone_configs.items():
        zone_configs[str(zone)] = {
            "available_modes_mask": hex(cfg.available_modes_mask),
            "fan_array": [hex(v) for v in cfg.fan_array],
            "min_cool_sp": cfg.min_cool_sp,
            "max_cool_sp": cfg.max_cool_sp,
            "min_heat_sp": cfg.min_heat_sp,
            "max_heat_sp": cfg.max_heat_sp,
        }

    zone_states = {}
    state = coordinator.thermostat_state
    if state:
        for zone, zs in state.zones.items():
            zone_states[str(zone)] = {
                "ambient_temp": zs.ambient_temp,
                "mode_num": zs.mode_num,
                "cool_sp": zs.cool_sp,
                "heat_sp": zs.heat_sp,
                "auto_cool_sp": zs.auto_cool_sp,
                "auto_heat_sp": zs.auto_heat_sp,
                "fan_only_speed": zs.fan_only_speed,
                "cool_fan_speed": zs.cool_fan_speed,
                "electric_fan_speed": zs.electric_fan_speed,
                "gas_fan_speed": zs.gas_fan_speed,
                "auto_fan_speed": zs.auto_fan_speed,
                "cycle_active": zs.cycle_active,
                "is_cooling": zs.is_cooling,
                "is_heating": zs.is_heating,
                "fault": zs.fault,
            }

    return {
        "address": coordinator.address,
        "connected": coordinator.connected,
        "authenticated": coordinator.authenticated,
        "data_healthy": coordinator.data_healthy,
        "device_info": {
            "serial_number": coordinator.serial_number,
            "firmware_version": coordinator.firmware_version,
            "device_type": coordinator.device_type,
            "device_model": coordinator.device_model,
            "config_index": coordinator.config_index,
            "manufacturer_name": coordinator.manufacturer_name,
            "ble_model_number": coordinator.ble_model_number,
        },
        "system_power": state.system_power if state else None,
        "available_zones": state.available_zones if state else [],
        "zone_configs": zone_configs,
        "zone_states": zone_states,
    }
