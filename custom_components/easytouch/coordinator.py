"""Coordinator for EasyTouch BLE thermostat communication.

Connection lifecycle:
  1. Connect via HA Bluetooth (supports ESPHome BT proxy)
  2. Enable notifications on JSON_RET (FF01)
  3. Write password (raw UTF-8 bytes) to PASSWORD_CHAR (DD01); GATT_SUCCESS = authenticated
  4. Request Get Config for zones 0-3 (300 ms apart) — discover zone capabilities
  5. Start 4-second poll loop (Get Status zone 0 → read/notify response)
  6. Parse Z_sts response → update ZoneState per zone
  7. Entities pull state on coordinator update

Reference: EasyTouchDevicePlugin.kt + EasyTouchConstants.kt
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from bleak import BleakClient, BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    AUTH_STEP_DELAY_S,
    CONFIG_ZONE_DELAY_S,
    CONF_PASSWORD,
    DEBOUNCE_DELAY_S,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEVICE_INFO_SERVICE_UUID,
    DEVICE_TO_HA_MODE,
    DOMAIN,
    FAN_FIELD_AUTO,
    FAN_FIELD_COOL,
    FAN_FIELD_ELECTRIC,
    FAN_FIELD_FAN_ONLY,
    FAN_FIELD_GAS,
    FAN_VALUE_TO_HA,
    FIRMWARE_REVISION_UUID,
    GAS_MODES,
    HA_FAN_TO_VALUE,
    HA_TO_DEVICE_DEFAULT,
    HARDWARE_REVISION_UUID,
    HEAT_TYPE_PRESETS,
    IDX_AMBIENT_TEMP,
    IDX_AUTO_COOL_SP,
    IDX_AUTO_FAN_SPEED,
    IDX_AUTO_HEAT_SP,
    IDX_COOL_FAN_SPEED,
    IDX_COOL_SP,
    IDX_DRY_SP,
    IDX_ELECTRIC_FAN_SPEED,
    IDX_FAULT,
    IDX_FAN_ONLY_SPEED,
    IDX_GAS_FAN_SPEED,
    IDX_HEAT_SP,
    IDX_MODE,
    IDX_STATUS_FLAGS,
    JSON_CMD_UUID,
    JSON_RET_UUID,
    MANUFACTURER_NAME_UUID,
    MODEL_NUMBER_UUID,
    MODE_FURNACE,
    MODE_GAS_HEAT,
    MODE_HEAT,
    PASSWORD_CHAR_UUID,
    POLL_INTERVAL_S,
    READ_RESPONSE_DELAY_S,
    READ_RETRY_DELAY_S,
    RECONNECT_BACKOFF_BASE_S,
    RECONNECT_BACKOFF_CAP_S,
    SERIAL_NUMBER_UUID,
    SERVICE_UUID,
    FLAG_CYCLE_ACTIVE,
    FLAG_IS_COOLING,
    FLAG_IS_HEATING,
    STALE_TIMEOUT_S,
    STATUS_SUPPRESS_S,
)
from .models import ThermostatState, ZoneConfig, ZoneState

_LOGGER = logging.getLogger(__name__)


def _fan_field_for_mode(mode_num: int) -> str:
    """Return the JSON fan-field name for the given device mode number."""
    if mode_num in (5, 7, 12):   # heat_pump, heat_strip, electric_heat
        return FAN_FIELD_ELECTRIC
    if mode_num in (3, 4, 13):   # heat, furnace, gas_heat
        return FAN_FIELD_GAS
    return FAN_FIELD_COOL


def _fan_value_to_ha(value: int, fan_only: bool = False) -> str:
    if fan_only:
        return {0: "off", 1: "low", 2: "high"}.get(value, "off")
    return FAN_VALUE_TO_HA.get(value, "auto")


class EasyTouchCoordinator(DataUpdateCoordinator[ThermostatState | None]):
    """Coordinate BLE communication with an EasyTouch thermostat."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.unique_id}",
            update_interval=None,  # push-based; we drive the poll loop ourselves
        )
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self._password: str = entry.data.get(CONF_PASSWORD, "")

        self._client: BleakClient | None = None
        self._connected = False
        self._authenticated = False
        self._connect_lock = asyncio.Lock()

        # Zone configurations discovered via Get Config
        self.zone_configs: dict[int, ZoneConfig] = {}
        # Latest thermostat state
        self.thermostat_state: ThermostatState | None = None

        # Device info parsed from BLE 0x180A or status response
        self.manufacturer_name: str | None = None
        self.ble_model_number: str | None = None
        self.serial_number: str | None = None
        self.firmware_version: str | None = None
        self.hardware_revision: str | None = None
        self.device_model: str | None = None    # first 3 chars of serial
        self.device_type: str | None = None     # TT field from status
        self.config_index: str | None = None    # CI field from status

        # JSON response buffering (BLE MTU fragments the response)
        self._response_buffer: str = ""
        # Status suppression after commands
        self._suppress_until: float = 0.0
        # Per-zone debounce tasks (temperature and fan)
        self._debounce_tasks: dict[str, asyncio.Task] = {}

        # Signals
        self._config_done_event = asyncio.Event()   # set when all zone configs received
        self._poll_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._consecutive_failures: int = 0
        self._last_data_time: float = 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # Public helpers
    # ──────────────────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected and self._client is not None

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def data_healthy(self) -> bool:
        if not self._authenticated or self._last_data_time == 0.0:
            return False
        return (time.monotonic() - self._last_data_time) < 15.0

    def is_status_suppressed(self) -> bool:
        return time.monotonic() < self._suppress_until

    def suppress_status(self, seconds: float = STATUS_SUPPRESS_S) -> None:
        self._suppress_until = time.monotonic() + seconds

    def get_available_hvac_modes(self, zone: int) -> list[str]:
        """Return HA HVAC modes available for a zone (from MAV bitmask)."""
        cfg = self.zone_configs.get(zone)
        if cfg is None or cfg.available_modes_mask == 0:
            return ["off", "heat", "cool", "auto", "fan_only", "dry"]
        mav = cfg.available_modes_mask
        modes = ["off"]
        seen_ha: set[str] = {"off"}
        for bit in range(16):
            if mav & (1 << bit):
                ha = DEVICE_TO_HA_MODE.get(bit)
                if ha and ha not in seen_ha:
                    modes.append(ha)
                    seen_ha.add(ha)
        return modes

    def get_available_presets(self, zone: int) -> list[str]:
        """Return heat-source preset names available for a zone."""
        cfg = self.zone_configs.get(zone)
        if cfg is None or cfg.available_modes_mask == 0:
            return []
        mav = cfg.available_modes_mask
        return [
            name
            for name, mode_num in HEAT_TYPE_PRESETS.items()
            if mav & (1 << mode_num)
        ]

    def get_available_fan_modes(self, zone: int, mode_num: int) -> list[str]:
        """Return HA fan modes available for a zone/mode combo (from FA array)."""
        if mode_num in GAS_MODES:
            return ["auto"]
        cfg = self.zone_configs.get(zone)
        if cfg is None or mode_num < 0 or mode_num >= len(cfg.fan_array):
            return ["auto", "low", "high"]
        bitmask = cfg.fan_array[mode_num]
        if bitmask == 0:
            return []
        max_speed = bitmask & 0x0F
        fixed_speed = bool(bitmask & 0x10)
        allow_manual_auto = bool(bitmask & 0x40)
        allow_full_auto = bool(bitmask & 0x80)
        if fixed_speed and max_speed == 0:
            return []
        modes: list[str] = []
        if max_speed >= 2:
            modes += ["low", "high"]
        elif max_speed == 1:
            modes.append("low")
        if allow_full_auto or allow_manual_auto:
            modes.append("auto")
        return modes or ["auto"]

    # ──────────────────────────────────────────────────────────────────────────
    # Command API
    # ──────────────────────────────────────────────────────────────────────────

    async def async_set_hvac_mode(self, zone: int, ha_mode: str) -> None:
        """Change HVAC mode (with optional heat-preset fallback)."""
        mode_num = HA_TO_DEVICE_DEFAULT.get(ha_mode, 0)
        if ha_mode == "heat":
            # Use first available heat preset if any
            presets = self.get_available_presets(zone)
            if presets:
                from .const import HEAT_TYPE_PRESETS as _P
                first_mode = _P.get(presets[0])
                if first_mode is not None:
                    mode_num = first_mode
        power = 0 if ha_mode == "off" else 1
        await self._send_change({"zone": zone, "power": power, "mode": mode_num})
        self.suppress_status()

    async def async_set_preset_mode(self, zone: int, preset: str) -> None:
        """Change heat-source preset (only valid when HVAC mode is heat)."""
        from .const import HEAT_TYPE_PRESETS as _P
        mode_num = _P.get(preset)
        if mode_num is None:
            _LOGGER.warning("Unknown preset: %s", preset)
            return
        await self._send_change({"zone": zone, "power": 1, "mode": mode_num})
        self.suppress_status()

    async def async_set_temperature(
        self,
        zone: int,
        temp: float,
        mode_num: int,
    ) -> None:
        """Set single target temperature (cool, heat, or dry mode)."""
        ha_mode = DEVICE_TO_HA_MODE.get(mode_num, "off")
        field_map = {"cool": "cool_sp", "heat": "heat_sp", "dry": "dry_sp"}
        field = field_map.get(ha_mode)
        if field is None:
            return
        await self._send_change({"zone": zone, field: int(temp)})
        self.suppress_status()

    async def async_set_temperature_high(self, zone: int, temp: float) -> None:
        """Set auto-mode cool setpoint (target_temperature_high)."""
        await self._send_change({"zone": zone, "autoCool_sp": int(temp)})
        self.suppress_status()

    async def async_set_temperature_low(self, zone: int, temp: float) -> None:
        """Set auto-mode heat setpoint (target_temperature_low)."""
        await self._send_change({"zone": zone, "autoHeat_sp": int(temp)})
        self.suppress_status()

    async def async_set_fan_mode(self, zone: int, ha_fan: str, mode_num: int) -> None:
        """Set fan mode; field depends on current HVAC mode."""
        fan_value = HA_FAN_TO_VALUE.get(ha_fan, 128)
        ha_mode = DEVICE_TO_HA_MODE.get(mode_num, "off")
        field_map = {
            "fan_only": FAN_FIELD_FAN_ONLY,
            "cool": FAN_FIELD_COOL,
            "heat": _fan_field_for_mode(mode_num),
            "auto": FAN_FIELD_AUTO,
        }
        field = field_map.get(ha_mode)
        if field is None:
            return
        await self._send_change({"zone": zone, field: fan_value})
        self.suppress_status()

    async def async_reboot(self) -> None:
        """Send reboot command (reset: " OK")."""
        await self._send_change({"zone": 0, "reset": " OK"})
        self.suppress_status(10.0)

    def schedule_debounce(
        self,
        key: str,
        coro_factory: Callable[[], Any],
        delay: float = DEBOUNCE_DELAY_S,
    ) -> None:
        """Cancel any pending debounce task for key and schedule a new one."""
        old = self._debounce_tasks.pop(key, None)
        if old and not old.done():
            old.cancel()

        async def _run() -> None:
            await asyncio.sleep(delay)
            await coro_factory()

        self._debounce_tasks[key] = self.hass.async_create_task(_run())

    # ──────────────────────────────────────────────────────────────────────────
    # BLE write helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _send_change(self, changes: dict) -> None:
        payload = json.dumps({"Type": "Change", "Changes": changes})
        await self._write_json(payload)

    async def _write_json(self, payload: str, attempt: int = 0) -> None:
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot write JSON: not connected")
            return
        delays = [0.1, 0.4, 0.6]
        await asyncio.sleep(delays[attempt] if attempt < len(delays) else 0.6)
        try:
            data = payload.encode("utf-8")
            await self._client.write_gatt_char(JSON_CMD_UUID, data, response=True)
            _LOGGER.debug("JSON write OK (attempt %d): %.100s", attempt + 1, payload)
        except BleakError as exc:
            if attempt < 2:
                _LOGGER.debug("JSON write failed (attempt %d): %s — retrying", attempt + 1, exc)
                await self._write_json(payload, attempt + 1)
            else:
                _LOGGER.warning("JSON write failed after 3 attempts: %s", exc)

    async def _request_config(self, zone: int) -> None:
        payload = json.dumps({"Type": "Get Config", "Zone": zone})
        await self._write_json(payload)

    async def _request_status(self) -> None:
        payload = json.dumps({"Type": "Get Status", "Zone": 0, "EM": "x", "TM": 0})
        await self._write_json(payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Connection lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    async def async_connect(self) -> None:
        async with self._connect_lock:
            if self._connected:
                return
            await self._do_connect()

    async def async_disconnect(self) -> None:
        self._stop_poll()
        self._cancel_reconnect()
        self._connected = False
        self._authenticated = False
        if self._client:
            try:
                await self._client.disconnect()
            except BleakError:
                pass
            self._client = None

    async def _do_connect(self) -> None:
        for attempt in range(1, 4):
            try:
                await self._try_connect(attempt)
                return
            except Exception as exc:
                _LOGGER.warning("Connect attempt %d/3 failed: %s", attempt, exc)
                if self._client:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None
                self._connected = False
                self._authenticated = False
                if attempt < 3:
                    await asyncio.sleep(3 * attempt)
        raise BleakError(f"All connection attempts to {self.address} failed")

    async def _try_connect(self, attempt: int) -> None:
        _LOGGER.info("Connecting to EasyTouch %s (attempt %d)", self.address, attempt)
        device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            raise BleakError(f"EasyTouch {self.address} not found by HA Bluetooth")

        client = await establish_connection(
            BleakClient,
            device,
            self.address,
            disconnected_callback=self._on_disconnect,
        )
        await self._finish_connect(client)

    async def _finish_connect(self, client: BleakClient) -> None:
        self._client = client
        self._connected = True
        _LOGGER.info("Connected to EasyTouch %s", self.address)

        # Brief settle
        await asyncio.sleep(AUTH_STEP_DELAY_S)

        # Enable notifications on JSON_RET (FF01)
        try:
            await client.start_notify(JSON_RET_UUID, self._on_notify)
            _LOGGER.debug("Notifications enabled on FF01")
        except BleakError as exc:
            _LOGGER.warning("Failed to enable notifications on FF01: %s — will use reads", exc)

        # Try reading Device Information Service
        await self._read_device_info(client)

        # Authenticate: write password bytes to DD01
        await asyncio.sleep(AUTH_STEP_DELAY_S)
        await self._authenticate(client)

    async def _read_device_info(self, client: BleakClient) -> None:
        """Read BLE 0x180A Device Information Service if present."""
        try:
            svc = client.services.get_service(DEVICE_INFO_SERVICE_UUID)
            if svc is None:
                return
            reads = {
                MANUFACTURER_NAME_UUID: "manufacturer_name",
                MODEL_NUMBER_UUID: "ble_model_number",
                SERIAL_NUMBER_UUID: "serial_number",
                FIRMWARE_REVISION_UUID: "firmware_version",
                HARDWARE_REVISION_UUID: "hardware_revision",
            }
            for uuid, attr in reads.items():
                char = svc.get_characteristic(uuid)
                if char is None:
                    continue
                try:
                    data = await client.read_gatt_char(uuid)
                    value = data.decode("utf-8", errors="replace").strip()
                    setattr(self, attr, value)
                    _LOGGER.debug("Device info %s: %s", attr, value)
                except BleakError:
                    pass
            if self.serial_number and len(self.serial_number) >= 3:
                self.device_model = self.serial_number[:3]
        except Exception as exc:
            _LOGGER.debug("Could not read Device Information Service: %s", exc)

    async def _authenticate(self, client: BleakClient) -> None:
        """Write password bytes to DD01; success = GATT_SUCCESS = authenticated."""
        try:
            pw_bytes = self._password.encode("utf-8")
            await client.write_gatt_char(PASSWORD_CHAR_UUID, pw_bytes, response=True)
            _LOGGER.info("Password write succeeded — authenticated")
            self._authenticated = True
            self._consecutive_failures = 0
            # Discover zone configs, then start polling
            self.hass.async_create_task(self._config_discovery())
        except BleakError as exc:
            _LOGGER.error("Password write failed: %s", exc)
            raise

    # ──────────────────────────────────────────────────────────────────────────
    # Config discovery (zones 0-3)
    # ──────────────────────────────────────────────────────────────────────────

    async def _config_discovery(self) -> None:
        """Request Get Config for zones 0-3 (300 ms apart), then start polling."""
        _LOGGER.info("Starting zone config discovery...")
        for zone in range(4):
            if not self._connected:
                break
            await self._request_config(zone)
            # Wait for response + gap before next zone
            await asyncio.sleep(CONFIG_ZONE_DELAY_S + 0.5)

        _LOGGER.info(
            "Config discovery done. Zones found: %s",
            sorted(self.zone_configs.keys()),
        )
        self._config_done_event.set()
        self._start_poll()

    # ──────────────────────────────────────────────────────────────────────────
    # Poll loop
    # ──────────────────────────────────────────────────────────────────────────

    def _start_poll(self) -> None:
        self._stop_poll()
        self._poll_task = self.hass.async_create_task(self._poll_loop())
        _LOGGER.info("Poll loop started (%.0fs interval)", POLL_INTERVAL_S)

    def _stop_poll(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self) -> None:
        """Write Get Status every 4 seconds; also do an explicit read for reliability."""
        try:
            while self._connected and self._authenticated:
                # Check for stale connection
                if (
                    self._last_data_time > 0
                    and (time.monotonic() - self._last_data_time) > STALE_TIMEOUT_S
                ):
                    _LOGGER.warning(
                        "No data for %.0fs — connection stale, reconnecting",
                        STALE_TIMEOUT_S,
                    )
                    await self._client.disconnect()
                    break

                await self._request_status()

                # Give notification time to arrive; also read explicitly as fallback
                await asyncio.sleep(READ_RESPONSE_DELAY_S)
                if self._client and self._connected:
                    try:
                        data = await self._client.read_gatt_char(JSON_RET_UUID)
                        if data:
                            self._accumulate(data.decode("utf-8", errors="replace"))
                    except BleakError:
                        pass

                # Wait the remainder of the poll interval
                await asyncio.sleep(POLL_INTERVAL_S - READ_RESPONSE_DELAY_S)
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Poll loop error")
        _LOGGER.debug("Poll loop exited")

    # ──────────────────────────────────────────────────────────────────────────
    # Notification + read data handler
    # ──────────────────────────────────────────────────────────────────────────

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        chunk = bytes(data).decode("utf-8", errors="replace")
        _LOGGER.debug("FF01 notify: %.100s", chunk)
        self._accumulate(chunk)

    def _accumulate(self, chunk: str) -> None:
        """Accumulate JSON chunks and process when complete."""
        self._response_buffer += chunk
        buf = self._response_buffer
        try:
            obj = json.loads(buf)
            self._response_buffer = ""
            self._handle_json(obj)
        except json.JSONDecodeError:
            # Incomplete — wait for more data
            pass

    def _handle_json(self, obj: dict) -> None:
        rtype = obj.get("Type", "")
        rt = obj.get("RT", "")

        if (rtype == "Response" and rt == "Config") or rtype == "Config":
            self._parse_config(obj)
        elif (rtype == "Response" and rt == "Status") or rtype == "Status":
            self._parse_status(obj)
        elif rtype in ("Change Result",) or (rtype == "Response" and rt == "Change"):
            success = obj.get("Success", False)
            _LOGGER.debug("Change result: success=%s", success)
        else:
            _LOGGER.debug("Unknown response Type=%s RT=%s", rtype, rt)

    # ──────────────────────────────────────────────────────────────────────────
    # Config parsing
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_config(self, obj: dict) -> None:
        cfg = obj.get("CFG")
        if cfg is None:
            _LOGGER.debug("No CFG in config response")
            return

        zone = cfg.get("Zone", 0)
        mav = cfg.get("MAV", 0)
        fa_raw = cfg.get("FA", [])
        fa = list(fa_raw[:16]) + [0] * max(0, 16 - len(fa_raw))

        spl = cfg.get("SPL", [])
        min_cool = spl[0] if len(spl) > 0 else DEFAULT_MIN_TEMP
        max_cool = spl[1] if len(spl) > 1 else DEFAULT_MAX_TEMP
        min_heat = spl[2] if len(spl) > 2 else DEFAULT_MIN_TEMP
        max_heat = spl[3] if len(spl) > 3 else DEFAULT_MAX_TEMP

        self.zone_configs[zone] = ZoneConfig(
            zone=zone,
            available_modes_mask=mav,
            fan_array=fa,
            min_cool_sp=min_cool,
            max_cool_sp=max_cool,
            min_heat_sp=min_heat,
            max_heat_sp=max_heat,
        )
        _LOGGER.info(
            "Zone %d config: MAV=0x%X, cool=%d-%d°F, heat=%d-%d°F, fa=%s",
            zone, mav, min_cool, max_cool, min_heat, max_heat,
            [hex(v) for v in fa[:8]],
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Status parsing
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_status(self, obj: dict) -> None:
        self._last_data_time = time.monotonic()

        # Extract device info if not yet known
        self._update_device_info_from_status(obj)

        z_sts = obj.get("Z_sts")
        if not z_sts:
            _LOGGER.warning("No Z_sts in status response")
            return

        # System power from PRM array
        prm = obj.get("PRM", [])
        system_power = bool(prm[1] & 0x08) if len(prm) > 1 else True

        zones: dict[int, ZoneState] = {}
        for key, arr in z_sts.items():
            zone_num = int(key) if key.isdigit() else None
            if zone_num is None:
                continue
            if not isinstance(arr, list) or len(arr) < 12:
                _LOGGER.warning("Invalid zone %s data", key)
                continue

            def _i(idx: int) -> int:
                return int(arr[idx]) if idx < len(arr) else 0

            flags = _i(IDX_STATUS_FLAGS)
            zones[zone_num] = ZoneState(
                zone=zone_num,
                ambient_temp=_i(IDX_AMBIENT_TEMP),
                cool_sp=_i(IDX_COOL_SP),
                heat_sp=_i(IDX_HEAT_SP),
                auto_cool_sp=_i(IDX_AUTO_COOL_SP),
                auto_heat_sp=_i(IDX_AUTO_HEAT_SP),
                dry_sp=_i(IDX_DRY_SP),
                mode_num=_i(IDX_MODE),
                fan_only_speed=_i(IDX_FAN_ONLY_SPEED),
                cool_fan_speed=_i(IDX_COOL_FAN_SPEED),
                electric_fan_speed=_i(IDX_ELECTRIC_FAN_SPEED),
                auto_fan_speed=_i(IDX_AUTO_FAN_SPEED),
                gas_fan_speed=_i(IDX_GAS_FAN_SPEED),
                fault=_i(IDX_FAULT),
                cycle_active=bool(flags & FLAG_CYCLE_ACTIVE),
                is_cooling=bool(flags & FLAG_IS_COOLING),
                is_heating=bool(flags & FLAG_IS_HEATING),
            )

        available_zones = sorted(zones.keys())
        _LOGGER.debug(
            "Status: zones=%s, system_power=%s", available_zones, system_power
        )

        if self.is_status_suppressed():
            _LOGGER.debug("Status suppressed (command in progress)")
            return

        self.thermostat_state = ThermostatState(
            available_zones=available_zones,
            zones=zones,
            system_power=system_power,
            serial_number=self.serial_number,
            firmware_version=self.firmware_version,
            device_type=self.device_type,
            config_index=self.config_index,
            model_number=self.device_model,
        )
        self.async_set_updated_data(self.thermostat_state)

    def _update_device_info_from_status(self, obj: dict) -> None:
        changed = False
        sn = obj.get("SN")
        if sn and not self.serial_number:
            self.serial_number = str(sn)
            if len(self.serial_number) >= 3 and not self.device_model:
                self.device_model = self.serial_number[:3]
            changed = True
        rev = obj.get("REV")
        if rev and not self.firmware_version:
            self.firmware_version = str(rev)
            changed = True
        tt = obj.get("TT")
        if tt and not self.device_type:
            self.device_type = str(tt)
            changed = True
        ci = obj.get("CI")
        if ci is not None and not self.config_index:
            self.config_index = str(ci)
            changed = True
        if changed:
            _LOGGER.debug(
                "Device info updated: sn=%s fw=%s tt=%s ci=%s model=%s",
                self.serial_number, self.firmware_version,
                self.device_type, self.config_index, self.device_model,
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Disconnect + reconnect
    # ──────────────────────────────────────────────────────────────────────────

    @callback
    def _on_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.warning("EasyTouch %s disconnected", self.address)
        self._stop_poll()
        self._connected = False
        self._authenticated = False
        self._config_done_event.clear()
        self._schedule_reconnect()
        # Push unavailable state
        self.async_set_updated_data(None)

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return
        delay = min(
            RECONNECT_BACKOFF_BASE_S * (2 ** self._consecutive_failures),
            RECONNECT_BACKOFF_CAP_S,
        )
        self._consecutive_failures += 1
        _LOGGER.info(
            "Scheduling reconnect in %.0fs (attempt %d)",
            delay, self._consecutive_failures,
        )
        self._reconnect_task = self.hass.async_create_task(
            self._reconnect_with_delay(delay)
        )

    async def _reconnect_with_delay(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._connected:
                return
            _LOGGER.info("Reconnecting to %s...", self.address)
            await self.async_connect()
            self._consecutive_failures = 0
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _LOGGER.warning("Reconnect failed: %s", exc)
            self._schedule_reconnect()

    def _cancel_reconnect(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None

    # ──────────────────────────────────────────────────────────────────────────
    # DataUpdateCoordinator hook (no-op: we push via async_set_updated_data)
    # ──────────────────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> ThermostatState | None:
        if not self._connected:
            try:
                await self.async_connect()
            except BleakError as exc:
                _LOGGER.warning("Reconnect during update failed: %s", exc)
        return self.thermostat_state

    # ──────────────────────────────────────────────────────────────────────────
    # Config-done wait (for platform setup)
    # ──────────────────────────────────────────────────────────────────────────

    async def async_wait_for_config(self, timeout: float = 30.0) -> bool:
        """Wait up to `timeout` seconds for zone config discovery to complete."""
        try:
            await asyncio.wait_for(
                asyncio.shield(self._config_done_event.wait()), timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            return False
