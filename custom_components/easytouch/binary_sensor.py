"""Binary sensor entities for EasyTouch thermostat (diagnostic)."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EasyTouchCoordinator
from .models import ThermostatState


@dataclass(frozen=True, kw_only=True)
class EasyTouchBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: callable = lambda coordinator: False


BINARY_SENSOR_DESCRIPTIONS: tuple[EasyTouchBinarySensorDescription, ...] = (
    EasyTouchBinarySensorDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_registry_enabled_default=True,
        entity_category="diagnostic",
        value_fn=lambda c: c.connected,
    ),
    EasyTouchBinarySensorDescription(
        key="authenticated",
        translation_key="authenticated",
        device_class=None,
        entity_registry_enabled_default=True,
        entity_category="diagnostic",
        value_fn=lambda c: c.authenticated,
    ),
    EasyTouchBinarySensorDescription(
        key="data_healthy",
        translation_key="data_healthy",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_registry_enabled_default=True,
        entity_category="diagnostic",
        value_fn=lambda c: not c.data_healthy,  # PROBLEM = True when unhealthy
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EasyTouchCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EasyTouchBinarySensor(coordinator, entry, desc)
        for desc in BINARY_SENSOR_DESCRIPTIONS
    )


class EasyTouchBinarySensor(
    CoordinatorEntity[EasyTouchCoordinator], BinarySensorEntity
):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EasyTouchCoordinator,
        entry: ConfigEntry,
        description: EasyTouchBinarySensorDescription,
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
    def is_on(self) -> bool | None:
        return self._description.value_fn(self.coordinator)
