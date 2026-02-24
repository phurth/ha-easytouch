"""Climate entities for EasyTouch thermostat zones.

One ClimateEntity is created per discovered zone.  Zones are discovered
dynamically: the coordinator fires updates as Get Config responses arrive,
and climate.py adds entities the first time a zone appears in zone_configs.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEVICE_TO_HA_MODE,
    DOMAIN,
    GAS_MODES,
    HA_TO_DEVICE_DEFAULT,
    HEAT_TYPE_PRESETS,
    HEAT_TYPE_REVERSE,
    TEMP_STEP,
)
from .coordinator import EasyTouchCoordinator
from .models import ThermostatState, ZoneState

_LOGGER = logging.getLogger(__name__)

# HVACMode string → HA constant
_HA_MODE = {
    "off": HVACMode.OFF,
    "cool": HVACMode.COOL,
    "heat": HVACMode.HEAT,
    "fan_only": HVACMode.FAN_ONLY,
    "dry": HVACMode.DRY,
    "auto": HVACMode.AUTO,
}

# HVACAction string → HA constant
_HA_ACTION = {
    "cooling": HVACAction.COOLING,
    "heating": HVACAction.HEATING,
    "fan": HVACAction.FAN,
    "idle": HVACAction.IDLE,
    "off": HVACAction.OFF,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EasyTouch climate entities, adding new ones as zones are discovered."""
    coordinator: EasyTouchCoordinator = hass.data[DOMAIN][entry.entry_id]
    created_zones: set[int] = set()

    @callback
    def _check_new_zones() -> None:
        # Only create entities for zones confirmed in the status response (Z_sts).
        # Get Config may return data for zones 1-3 even on single-zone devices.
        state = coordinator.thermostat_state
        if state is None:
            return
        new = [
            EasyTouchClimate(coordinator, entry, zone)
            for zone in state.available_zones
            if zone not in created_zones
        ]
        if new:
            for entity in new:
                created_zones.add(entity.zone)
                _LOGGER.info("Adding climate entity for zone %d", entity.zone)
            async_add_entities(new)

    # Check immediately (in case config discovery already completed)
    _check_new_zones()
    # And on every coordinator update (new zones may appear)
    entry.async_on_unload(coordinator.async_add_listener(_check_new_zones))


class EasyTouchClimate(CoordinatorEntity[EasyTouchCoordinator], ClimateEntity):
    """Climate entity for a single EasyTouch thermostat zone."""

    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_target_temperature_step = TEMP_STEP
    _attr_has_entity_name = True
    _attr_translation_key = "zone"

    def __init__(
        self,
        coordinator: EasyTouchCoordinator,
        entry: ConfigEntry,
        zone: int,
    ) -> None:
        super().__init__(coordinator)
        self.zone = zone
        self._entry = entry
        address = entry.data[CONF_ADDRESS]
        mac_clean = address.replace(":", "").lower()

        # Unique ID and entity name
        self._attr_unique_id = f"easytouch_{mac_clean}_zone_{zone}"
        if zone == 0:
            self._attr_name = None  # Use device name for single zone
        else:
            self._attr_name = f"Zone {zone + 1}"

        # Device info (shared across all entities for this thermostat)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=entry.data.get("name") or f"EasyTouch {address}",
            manufacturer="Micro-Air",
            model="EasyTouch RV Thermostat",
        )

        # Supported features (will be updated dynamically)
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.PRESET_MODE
        )

        # Initialise attr caches
        self._attr_hvac_modes = [HVACMode.OFF]
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_hvac_action = HVACAction.OFF
        self._attr_fan_modes = ["auto", "low", "high"]
        self._attr_fan_mode = "auto"
        self._attr_preset_modes: list[str] | None = None
        self._attr_preset_mode: str | None = None
        self._attr_current_temperature: float | None = None
        self._attr_target_temperature: float | None = None
        self._attr_target_temperature_high: float | None = None
        self._attr_target_temperature_low: float | None = None
        self._attr_min_temp = DEFAULT_MIN_TEMP
        self._attr_max_temp = DEFAULT_MAX_TEMP

    # ── State refresh ─────────────────────────────────────────────────────────

    @callback
    def _handle_coordinator_update(self) -> None:
        state: ThermostatState | None = self.coordinator.data
        if state is None:
            self._attr_available = False
            self.async_write_ha_state()
            return

        zone_state: ZoneState | None = state.zones.get(self.zone)
        if zone_state is None:
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._attr_available = True
        self._update_from_zone(zone_state, state.system_power)
        self.async_write_ha_state()

    def _update_from_zone(self, zs: ZoneState, system_power: bool) -> None:
        """Rebuild all climate attributes from a ZoneState."""
        # HVAC mode
        if not system_power:
            ha_mode_str = "off"
        else:
            ha_mode_str = DEVICE_TO_HA_MODE.get(zs.mode_num, "off")
        self._attr_hvac_mode = _HA_MODE.get(ha_mode_str, HVACMode.OFF)

        # Available HVAC modes from MAV bitmask
        avail_str = self.coordinator.get_available_hvac_modes(self.zone)
        self._attr_hvac_modes = [_HA_MODE[m] for m in avail_str if m in _HA_MODE]
        if HVACMode.OFF not in self._attr_hvac_modes:
            self._attr_hvac_modes.insert(0, HVACMode.OFF)

        # Temperature limits from zone config
        cfg = self.coordinator.zone_configs.get(self.zone)
        if cfg:
            self._attr_min_temp = min(cfg.min_cool_sp, cfg.min_heat_sp)
            self._attr_max_temp = max(cfg.max_cool_sp, cfg.max_heat_sp)

        # Current temperature
        self._attr_current_temperature = float(zs.ambient_temp)

        # Target temperature(s)
        if ha_mode_str == "cool":
            self._attr_target_temperature = float(zs.cool_sp)
            self._attr_target_temperature_high = None
            self._attr_target_temperature_low = None
        elif ha_mode_str == "heat":
            self._attr_target_temperature = float(zs.heat_sp)
            self._attr_target_temperature_high = None
            self._attr_target_temperature_low = None
        elif ha_mode_str == "dry":
            self._attr_target_temperature = float(zs.dry_sp)
            self._attr_target_temperature_high = None
            self._attr_target_temperature_low = None
        elif ha_mode_str == "auto":
            self._attr_target_temperature = None
            self._attr_target_temperature_high = float(zs.auto_cool_sp)
            self._attr_target_temperature_low = float(zs.auto_heat_sp)
        else:
            self._attr_target_temperature = None
            self._attr_target_temperature_high = None
            self._attr_target_temperature_low = None

        # Fan mode
        avail_fan = self.coordinator.get_available_fan_modes(self.zone, zs.mode_num)
        self._attr_fan_modes = avail_fan if avail_fan else ["auto"]

        if zs.mode_num in GAS_MODES:
            fan_str = "auto"
        elif ha_mode_str == "fan_only":
            raw = {0: "off", 1: "low", 2: "high"}.get(zs.fan_only_speed, "off")
            fan_str = raw if raw != "off" else "auto"
        elif ha_mode_str == "cool":
            fan_str = _fa_to_ha(zs.cool_fan_speed)
        elif ha_mode_str == "heat":
            if zs.mode_num in (5, 7, 12):   # heat_pump, heat_strip, electric_heat
                fan_str = _fa_to_ha(zs.electric_fan_speed)
            else:
                fan_str = _fa_to_ha(zs.gas_fan_speed)
        elif ha_mode_str == "auto":
            fan_str = _fa_to_ha(zs.auto_fan_speed)
        else:
            fan_str = _fa_to_ha(zs.auto_fan_speed)

        if fan_str == "off":
            fan_str = "auto"
        if fan_str not in self._attr_fan_modes:
            fan_str = self._attr_fan_modes[0] if self._attr_fan_modes else "auto"
        self._attr_fan_mode = fan_str

        # Preset mode (heat source selection)
        presets = self.coordinator.get_available_presets(self.zone)
        self._attr_preset_modes = presets if presets else None
        if presets and ha_mode_str in ("heat", "auto"):
            self._attr_preset_mode = HEAT_TYPE_REVERSE.get(zs.mode_num, presets[0])
        else:
            self._attr_preset_mode = None

        # HVAC action
        if not system_power or ha_mode_str == "off":
            action = HVACAction.OFF
        elif zs.is_cooling:
            action = HVACAction.COOLING
        elif zs.is_heating:
            action = HVACAction.HEATING
        elif zs.cycle_active and ha_mode_str == "fan_only":
            action = HVACAction.FAN
        else:
            action = HVACAction.IDLE
        self._attr_hvac_action = action

    # ── Optimistic state helper ────────────────────────────────────────────────

    def _optimistic_apply_mode(self, hvac_mode: HVACMode, mode_num: int) -> None:
        """Optimistically update entity attributes for an HVAC mode / mode_num transition.

        Called before sending a command so the UI reflects the new state immediately
        rather than bouncing back to the old state during the status-suppression window.
        """
        ha_str = hvac_mode.value
        self._attr_hvac_mode = hvac_mode
        self._attr_hvac_action = HVACAction.OFF if hvac_mode == HVACMode.OFF else HVACAction.IDLE

        # Fan modes for the new mode_num
        avail_fan = self.coordinator.get_available_fan_modes(self.zone, mode_num)
        self._attr_fan_modes = avail_fan if avail_fan else ["auto"]
        # For gas modes the fan is autonomous; otherwise keep current mode if still valid
        if mode_num in GAS_MODES or not self._attr_fan_modes:
            self._attr_fan_mode = "auto"
        elif self._attr_fan_mode not in self._attr_fan_modes:
            self._attr_fan_mode = self._attr_fan_modes[0]

        # Preset mode
        presets = self.coordinator.get_available_presets(self.zone)
        if presets and ha_str in ("heat", "auto"):
            self._attr_preset_mode = HEAT_TYPE_REVERSE.get(mode_num, presets[0])
        else:
            self._attr_preset_mode = None

    # ── Service handlers ──────────────────────────────────────────────────────

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        ha_str = hvac_mode.value
        # Mirror coordinator's mode_num selection so fan_modes are correct immediately
        mode_num = HA_TO_DEVICE_DEFAULT.get(ha_str, 0)
        if ha_str == "heat":
            presets = self.coordinator.get_available_presets(self.zone)
            if presets:
                first_mode = HEAT_TYPE_PRESETS.get(presets[0])
                if first_mode is not None:
                    mode_num = first_mode
        self._optimistic_apply_mode(hvac_mode, mode_num)
        self.async_write_ha_state()
        await self.coordinator.async_set_hvac_mode(self.zone, ha_str)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        mode_num = HEAT_TYPE_PRESETS.get(preset_mode)
        if mode_num is None:
            return
        self._optimistic_apply_mode(HVACMode.HEAT, mode_num)
        self._attr_preset_mode = preset_mode  # ensure exact string is used
        self.async_write_ha_state()
        await self.coordinator.async_set_preset_mode(self.zone, preset_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get("temperature")
        temp_high = kwargs.get("target_temp_high")
        temp_low = kwargs.get("target_temp_low")
        state = self.coordinator.thermostat_state
        mode_num = state.zones[self.zone].mode_num if state else 0

        # Optimistic update
        if temp_high is not None:
            self._attr_target_temperature_high = float(temp_high)
        if temp_low is not None:
            self._attr_target_temperature_low = float(temp_low)
        if temp is not None:
            self._attr_target_temperature = float(temp)
        self.async_write_ha_state()

        if temp_high is not None:
            self.coordinator.schedule_debounce(
                f"temp_high_{self.zone}",
                lambda t=temp_high: self.coordinator.async_set_temperature_high(self.zone, t),
            )
        if temp_low is not None:
            self.coordinator.schedule_debounce(
                f"temp_low_{self.zone}",
                lambda t=temp_low: self.coordinator.async_set_temperature_low(self.zone, t),
            )
        if temp is not None:
            self.coordinator.schedule_debounce(
                f"temp_{self.zone}",
                lambda t=temp: self.coordinator.async_set_temperature(self.zone, t, mode_num),
            )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if not fan_mode:
            _LOGGER.debug("Ignoring empty fan_mode request")
            return
        state = self.coordinator.thermostat_state
        mode_num = state.zones[self.zone].mode_num if state else 0
        # Optimistic update
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()
        self.coordinator.schedule_debounce(
            f"fan_{self.zone}",
            lambda fm=fan_mode, mn=mode_num: self.coordinator.async_set_fan_mode(
                self.zone, fm, mn
            ),
        )


def _fa_to_ha(value: int) -> str:
    from .const import FAN_VALUE_TO_HA
    return FAN_VALUE_TO_HA.get(value, "auto")
