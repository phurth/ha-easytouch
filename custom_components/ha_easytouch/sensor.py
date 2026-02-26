"""Diagnostic sensor entities for EasyTouch thermostat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EasyTouchCoordinator


@dataclass(frozen=True, kw_only=True)
class EasyTouchSensorDescription(SensorEntityDescription):
    value_fn: callable = lambda coordinator: None


SENSOR_DESCRIPTIONS: tuple[EasyTouchSensorDescription, ...] = (
    EasyTouchSensorDescription(
        key="serial_number",
        translation_key="serial_number",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=True,
        icon="mdi:identifier",
        value_fn=lambda c: c.serial_number,
    ),
    EasyTouchSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=True,
        icon="mdi:chip",
        value_fn=lambda c: c.firmware_version,
    ),
    EasyTouchSensorDescription(
        key="model_number",
        translation_key="model_number",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=True,
        icon="mdi:barcode",
        value_fn=lambda c: c.device_model,
    ),
    EasyTouchSensorDescription(
        key="device_type",
        translation_key="device_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=True,
        icon="mdi:thermostat",
        value_fn=lambda c: c.device_type,
    ),
    EasyTouchSensorDescription(
        key="config_index",
        translation_key="config_index",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=True,
        icon="mdi:cog",
        value_fn=lambda c: c.config_index,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EasyTouchCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EasyTouchSensor(coordinator, entry, desc)
        for desc in SENSOR_DESCRIPTIONS
    )


class EasyTouchSensor(CoordinatorEntity[EasyTouchCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EasyTouchCoordinator,
        entry: ConfigEntry,
        description: EasyTouchSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        address = entry.data[CONF_ADDRESS]
        mac_clean = address.replace(":", "").lower()
        self._attr_unique_id = f"easytouch_{mac_clean}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=entry.data.get("name") or f"EasyTouch {address}",
            manufacturer="Micro-Air",
            model="EasyTouch RV Thermostat",
        )
        self._description = description

    @property
    def native_value(self) -> Any:
        return self._description.value_fn(self.coordinator)
