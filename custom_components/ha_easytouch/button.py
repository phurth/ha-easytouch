"""Button entities for EasyTouch thermostat."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EasyTouchCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EasyTouchCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EasyTouchRebootButton(coordinator, entry)])


class EasyTouchRebootButton(ButtonEntity):
    """Button that reboots the thermostat firmware."""

    _attr_has_entity_name = True
    _attr_translation_key = "reboot"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart"

    def __init__(
        self,
        coordinator: EasyTouchCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        address = entry.data[CONF_ADDRESS]
        mac_clean = address.replace(":", "").lower()
        self._attr_unique_id = f"easytouch_{mac_clean}_reboot"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=entry.data.get("name") or f"EasyTouch {address}",
            manufacturer="Micro-Air",
            model="EasyTouch RV Thermostat",
        )

    async def async_press(self) -> None:
        await self._coordinator.async_reboot()
