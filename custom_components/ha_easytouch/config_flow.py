"""Config flow for the EasyTouch integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import CONF_NAME, CONF_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

_EASYTOUCH_NAME_PREFIX = "EasyTouch"


def _is_easytouch(info: BluetoothServiceInfoBleak) -> bool:
    name = info.name or ""
    return name.startswith(_EASYTOUCH_NAME_PREFIX)


class EasyTouchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flow for EasyTouch."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None

    # ── Bluetooth auto-discovery ──────────────────────────────────────────────

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered via Bluetooth."""
        _LOGGER.debug(
            "EasyTouch discovered: %s (%s)",
            discovery_info.name,
            discovery_info.address,
        )
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._address = discovery_info.address
        self._name = discovery_info.name or f"EasyTouch {discovery_info.address}"
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    # ── Manual add ────────────────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-initiated flow."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            self._address = address
            for info in async_discovered_service_info(self.hass):
                if info.address == address:
                    self._discovery_info = info
                    self._name = info.name or f"EasyTouch {address}"
                    break
            else:
                self._name = f"EasyTouch {address}"
            return await self.async_step_confirm()

        # Show list of visible EasyTouch devices
        devices: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass):
            if _is_easytouch(info):
                devices[info.address] = info.name or info.address

        if not devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(devices)}),
        )

    # ── Confirm + password ────────────────────────────────────────────────────

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect password and create the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input.get(CONF_PASSWORD, "")
            return self.async_create_entry(
                title=self._name or "EasyTouch",
                data={
                    CONF_ADDRESS: self._address,
                    CONF_NAME: self._name,
                    CONF_PASSWORD: password,
                },
            )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {vol.Optional(CONF_PASSWORD, default=""): str}
            ),
            errors=errors,
            description_placeholders={"name": self._name or "EasyTouch"},
        )
