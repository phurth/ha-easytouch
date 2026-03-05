"""Coordinator for EasyTouch BLE thermostat communication.

Device protocol (confirmed via Android plugin EasyTouchDevicePlugin.kt and HCI traces):
  The device maintains a persistent BLE connection — no intentional disconnects.

  Session flow:
    1. Connect → Auth (write password to DD01)
    2. Config discovery (once): write Get Config zone 0-3 to EE01 → read FF01 after each
    3. Poll loop: drain command queue → write Get Status to EE01 → read FF01 → sleep

  FF01 (0x002c): Read-only (0x02 properties). Notifications NOT supported.
  Responses are obtained by reading FF01 ~100ms after each EE01 write.

  Commands are queued in _command_queue (asyncio.Queue). The poll loop drains the queue
  before each status poll, guaranteeing delivery within at most POLL_INTERVAL_S.

  On unexpected disconnect: reconnect with exponential backoff. Config discovery is
  skipped on reconnect when already completed (_config_done == True).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    AUTH_STEP_DELAY_S,
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
    RECONNECT_BACKOFF_BASE_S,
    RECONNECT_BACKOFF_CAP_S,
    SERIAL_NUMBER_UUID,
    SERVICE_UUID,
    FLAG_CYCLE_ACTIVE,
    FLAG_IS_COOLING,
    FLAG_IS_HEATING,
    STATUS_SUPPRESS_S,
)
from .models import ThermostatState, ZoneConfig, ZoneState

_LOGGER = logging.getLogger(__name__)

# Delay between writing EE01 and reading FF01 — device needs time to prepare the response.
# Android plugin uses 50ms; 100ms is more conservative for ESPHome proxy paths.
_POST_WRITE_READ_DELAY_S = 0.10

# Delay between Get Config requests during zone discovery.
_INTER_CONFIG_DELAY_S = 0.30


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

        # Command queue — commands are put here by _send_change and drained by poll loop
        self._command_queue: asyncio.Queue[str] = asyncio.Queue()

        # Signals
        self._config_done_event = asyncio.Event()   # set when all zone configs received
        self._poll_task: asyncio.Task | None = None     # persistent poll loop
        self._reconnect_task: asyncio.Task | None = None  # pending reconnect
        self._consecutive_failures: int = 0
        self._last_data_time: float = 0.0
        # Config-discovery state
        self._next_config_zone: int = 0   # next zone index to request (0-3)
        self._config_done: bool = False   # True once all zone configs gathered

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
        if self._last_data_time == 0.0:
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
            try:
                await coro_factory()
            except Exception as exc:
                _LOGGER.debug("Debounce task error: %s", exc)

        self._debounce_tasks[key] = self.entry.async_create_background_task(
            self.hass, _run(), f"easytouch_debounce_{key}"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # BLE write helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _send_change(self, changes: dict) -> None:
        """Enqueue a Change command for delivery on the next poll iteration."""
        payload = json.dumps({"Type": "Change", "Changes": changes}, separators=(',', ':'))
        await self._command_queue.put(payload)
        _LOGGER.debug("Command enqueued: %.100s", payload)

    async def _write_json(self, payload: str) -> None:
        if not self._client or not self._connected:
            raise BleakError("Cannot write JSON: not connected")
        try:
            data = payload.encode("utf-8")
            await self._client.write_gatt_char(JSON_CMD_UUID, data, response=True)
            _LOGGER.debug("JSON write OK: %.100s", payload)
        except BleakError as exc:
            _LOGGER.warning("JSON write failed: %s", exc)
            raise

    async def _request_config(self, zone: int) -> None:
        payload = json.dumps({"Type": "Get Config", "Zone": zone}, separators=(',', ':'))
        await self._write_json(payload)

    async def _request_status(self) -> None:
        payload = json.dumps(
            {"Type": "Get Status", "Zone": 0, "EM": "x", "TM": 0},
            separators=(',', ':'),
        )
        await self._write_json(payload)

    async def _do_read_response(self) -> None:
        """Read FF01 and accumulate response data. Raises BleakError on failure."""
        if not self._client or not self._connected:
            raise BleakError("Cannot read response: not connected")
        data = await asyncio.wait_for(
            self._client.read_gatt_char(JSON_RET_UUID), timeout=5.0
        )
        if data:
            raw = data.decode("utf-8", errors="replace").strip()
            if raw:
                _LOGGER.debug("FF01: %.200s", raw)
                self._accumulate(raw)

    # ──────────────────────────────────────────────────────────────────────────
    # Connection lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Start BLE connection; schedule reconnect on failure.

        Call this from async_setup_entry instead of async_connect directly.
        Unlike async_connect (which raises on failure), async_start catches
        errors and schedules the reconnect backoff loop so the integration
        recovers automatically — e.g. when all proxy slots are occupied at boot.
        """
        try:
            await self.async_connect()
        except Exception as exc:
            exc_str = str(exc).lower()
            if "already shutdown" in exc_str or "not started" in exc_str:
                _LOGGER.debug("BT stack not ready at startup — retrying in 5s")
                self._schedule_reconnect(5.0)
                return
            self._consecutive_failures += 1
            backoff = min(
                RECONNECT_BACKOFF_BASE_S * (2 ** min(self._consecutive_failures, 10)),
                RECONNECT_BACKOFF_CAP_S,
            )
            _LOGGER.warning(
                "EasyTouch %s initial connect failed: %s — retry in %.0fs",
                self.address, exc, backoff,
            )
            self._schedule_reconnect(backoff)

    async def async_connect(self) -> None:
        async with self._connect_lock:
            if self._connected:
                return
            await self._do_connect()

    async def async_disconnect(self) -> None:
        # Cancel background tasks
        for task in [self._poll_task, self._reconnect_task]:
            if task and not task.done():
                task.cancel()
        self._poll_task = None
        self._reconnect_task = None
        self._connected = False
        self._authenticated = False
        self._config_done = False
        self._next_config_zone = 0
        if self._client:
            try:
                await self._client.disconnect()
            except BleakError:
                pass
            self._client = None

    async def _do_connect(self) -> None:
        last_exc: Exception = BleakError("no attempts made")
        for attempt in range(1, 4):
            try:
                await self._try_connect(attempt)
                return
            except Exception as exc:
                last_exc = exc
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
        raise BleakError(f"All connection attempts to {self.address} failed: {last_exc}") from last_exc

    async def _try_connect(self, attempt: int) -> None:
        _LOGGER.debug("Connecting to EasyTouch %s (attempt %d)", self.address, attempt)
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
        try:
            await asyncio.wait_for(self._finish_connect(client), timeout=25.0)
        except asyncio.TimeoutError:
            _LOGGER.warning("_finish_connect timed out for %s", self.address)
            try:
                await client.disconnect()
            except Exception:
                pass
            raise BleakError(f"Post-connect setup timed out for {self.address}")

    async def _finish_connect(self, client: BleakClient) -> None:
        self._client = client
        self._connected = True
        _LOGGER.debug("Connected to EasyTouch %s", self.address)
        await asyncio.sleep(AUTH_STEP_DELAY_S)
        await self._read_device_info(client)
        await asyncio.sleep(AUTH_STEP_DELAY_S)
        await self._authenticate(client)
        # Start the persistent poll loop as a background task so HA bootstrap
        # does not wait for it.
        self._poll_task = self.entry.async_create_background_task(
            self.hass, self._poll_loop(), "easytouch_poll_loop"
        )

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
                    data = await asyncio.wait_for(
                        client.read_gatt_char(uuid), timeout=3.0
                    )
                    value = data.decode("utf-8", errors="replace").strip()
                    setattr(self, attr, value)
                    _LOGGER.debug("Device info %s: %s", attr, value)
                except (BleakError, asyncio.TimeoutError):
                    pass
            if self.serial_number and len(self.serial_number) >= 3:
                self.device_model = self.serial_number[:3]
        except Exception as exc:
            _LOGGER.debug("Could not read Device Information Service: %s", exc)

    async def _authenticate(self, client: BleakClient) -> None:
        """Write password bytes to DD01 to authenticate."""
        try:
            pw_bytes = self._password.encode("utf-8")
            await client.write_gatt_char(PASSWORD_CHAR_UUID, pw_bytes, response=True)
            _LOGGER.debug("Password write succeeded")
            self._authenticated = True
            self._consecutive_failures = 0
        except BleakError as exc:
            _LOGGER.error("Password write failed: %s", exc)
            raise

    # ──────────────────────────────────────────────────────────────────────────
    # Persistent poll loop (Android plugin pattern)
    # ──────────────────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Persistent poll loop — runs for the lifetime of one BLE connection.

        Mirrors the Android plugin's statusPollRunnable:
          - Config discovery once (zones 0-3, same connection)
          - Drain command queue before each status poll
          - Write Get Status → read FF01 → sleep POLL_INTERVAL_S → repeat
        """
        try:
            # Config discovery — only on first-ever connection (or after reset)
            if not self._config_done:
                await self._discover_configs()
            else:
                _LOGGER.debug("Skipping config discovery (already complete)")

            # Poll loop
            while True:
                # 1. Drain command queue — deliver all pending commands before polling status
                while not self._command_queue.empty():
                    try:
                        cmd = self._command_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    # Re-arm suppression at actual send time (matching Android plugin pattern)
                    self.suppress_status()
                    try:
                        await self._write_json(cmd)
                        await asyncio.sleep(_POST_WRITE_READ_DELAY_S)
                        await self._do_read_response()
                    except (BleakError, asyncio.TimeoutError) as exc:
                        _LOGGER.warning("Command delivery failed: %s", exc)
                        raise  # exit loop; _on_disconnect will trigger reconnect

                # 2. Status poll
                try:
                    await self._request_status()
                    await asyncio.sleep(_POST_WRITE_READ_DELAY_S)
                    await self._do_read_response()
                except (BleakError, asyncio.TimeoutError) as exc:
                    _LOGGER.warning("Status poll failed: %s", exc)
                    raise

                # 3. Sleep until next poll
                await asyncio.sleep(POLL_INTERVAL_S)

        except asyncio.CancelledError:
            # Normal shutdown (async_disconnect or reconnect cancellation)
            return
        except Exception as exc:
            _LOGGER.warning("Poll loop ended unexpectedly: %s (%s)", type(exc).__name__, exc)
            # Ensure disconnected state; _on_disconnect may already have fired
            if self._connected:
                self._connected = False
                self._authenticated = False
                if self._client:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None
                self.async_set_updated_data(None)
                self._schedule_reconnect(1.0)

    async def _discover_configs(self) -> None:
        """Request zone configs 0-3 sequentially within the current connection.

        Mirrors Android plugin's config discovery: requestConfig(0) → response →
        requestConfig(1) → ... → startStatusPolling(), all on the same GATT connection.
        """
        for zone in range(4):
            _LOGGER.debug("Get Config zone %d", zone)
            try:
                await self._request_config(zone)
                self._next_config_zone = zone + 1
                await asyncio.sleep(_POST_WRITE_READ_DELAY_S)
                await self._do_read_response()
                await asyncio.sleep(_INTER_CONFIG_DELAY_S)
            except (BleakError, asyncio.TimeoutError) as exc:
                _LOGGER.warning("Get Config zone %d failed: %s — aborting, will retry on reconnect", zone, exc)
                raise  # abort discovery; reconnect will retry

        active_zones = sorted(z for z, cfg in self.zone_configs.items() if cfg.available_modes_mask != 0)
        _LOGGER.info(
            "Config discovery done. Active zones: %s (probed: %s)",
            active_zones,
            sorted(self.zone_configs.keys()),
        )
        self._config_done = True
        self._config_done_event.set()

    # ──────────────────────────────────────────────────────────────────────────
    # Notification + read data handler
    # ──────────────────────────────────────────────────────────────────────────

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
        self._connected = False
        self._authenticated = False
        _LOGGER.debug("EasyTouch %s disconnected", self.address)
        # Cancel the poll loop (it may already be exiting due to the same error)
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = None
        # Entities show unavailable until reconnected
        self.async_set_updated_data(None)
        # Schedule reconnect
        self._schedule_reconnect(1.0)

    def _schedule_reconnect(self, delay: float) -> None:
        """Schedule a reconnect attempt after `delay` seconds."""
        if self._reconnect_task and not self._reconnect_task.done():
            return  # already scheduled
        _MAX_FAILURES = 20
        if self._consecutive_failures >= _MAX_FAILURES:
            _LOGGER.error(
                "EasyTouch %s: giving up after %d failures. Reload integration to retry.",
                self.address, self._consecutive_failures,
            )
            return
        _LOGGER.debug("Reconnect in %.0fs", delay)
        self._reconnect_task = self.entry.async_create_background_task(
            self.hass, self._reconnect_with_delay(delay), "easytouch_reconnect"
        )

    async def _reconnect_with_delay(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._connected:
                return
            await self.async_connect()
            # _finish_connect sets _consecutive_failures = 0 via _authenticate
            self._consecutive_failures = 0
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            exc_str = str(exc).lower()
            if "already shutdown" in exc_str or "not started" in exc_str:
                # BT stack not yet ready — retry quickly, no penalty
                _LOGGER.debug("BT stack not ready — retrying in 5s")
                self._reconnect_task = None
                self._schedule_reconnect(5.0)
                return
            self._consecutive_failures += 1
            backoff = min(
                RECONNECT_BACKOFF_BASE_S * (2 ** min(self._consecutive_failures, 10)),
                RECONNECT_BACKOFF_CAP_S,
            )
            _LOGGER.warning("Reconnect failed (%s) — retry in %.0fs", exc, backoff)
            self._reconnect_task = None
            self._schedule_reconnect(backoff)

    # ──────────────────────────────────────────────────────────────────────────
    # DataUpdateCoordinator hook (no-op: we push via async_set_updated_data)
    # ──────────────────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> ThermostatState | None:
        # Push-based: we drive the poll loop ourselves via async_set_updated_data.
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
