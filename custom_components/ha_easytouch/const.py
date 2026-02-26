"""Constants for the EasyTouch integration."""

DOMAIN = "ha_easytouch"

CONF_ADDRESS = "address"
CONF_NAME = "name"
CONF_PASSWORD = "password"

# ── BLE UUIDs ────────────────────────────────────────────────────────────────
SERVICE_UUID = "000000ff-0000-1000-8000-00805f9b34fb"
PASSWORD_CHAR_UUID = "0000dd01-0000-1000-8000-00805f9b34fb"
JSON_CMD_UUID = "0000ee01-0000-1000-8000-00805f9b34fb"
JSON_RET_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"

# Standard Device Information Service (0x180A)
DEVICE_INFO_SERVICE_UUID = "0000180a-0000-1000-8000-00805f9b34fb"
MANUFACTURER_NAME_UUID = "00002a29-0000-1000-8000-00805f9b34fb"
MODEL_NUMBER_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
SERIAL_NUMBER_UUID = "00002a25-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
HARDWARE_REVISION_UUID = "00002a27-0000-1000-8000-00805f9b34fb"

# ── Device mode numbers ───────────────────────────────────────────────────────
MODE_OFF = 0
MODE_FAN_ONLY = 1
MODE_COOL = 2
MODE_HEAT = 3           # Generic heat (gas fan)
MODE_FURNACE = 4        # AquaHot / Diesel furnace (gas fan)
MODE_HEAT_PUMP = 5      # Heat pump (electric fan)
MODE_DRY = 6
MODE_HEAT_STRIP = 7     # Heat strip (electric fan)
MODE_AUTO = 8
MODE_AUTO_HS = 9        # Auto with heat strip backup
MODE_AUTO_HP = 10       # Auto with heat pump backup
MODE_AUTO_FURNACE = 11  # Auto with furnace backup
MODE_ELECTRIC_HEAT = 12
MODE_GAS_HEAT = 13      # Gas heat (gas fan)

# Furnace / gas modes whose fan is autonomous (always "auto" in HA)
GAS_MODES: frozenset[int] = frozenset({MODE_HEAT, MODE_FURNACE, MODE_GAS_HEAT})

# ── HA mode mappings ──────────────────────────────────────────────────────────
DEVICE_TO_HA_MODE: dict[int, str] = {
    MODE_OFF: "off",
    MODE_FAN_ONLY: "fan_only",
    MODE_COOL: "cool",
    MODE_HEAT: "heat",
    MODE_FURNACE: "heat",
    MODE_HEAT_PUMP: "heat",
    MODE_DRY: "dry",
    MODE_HEAT_STRIP: "heat",
    MODE_AUTO: "auto",
    MODE_AUTO_HS: "auto",
    MODE_AUTO_HP: "auto",
    MODE_AUTO_FURNACE: "auto",
    MODE_ELECTRIC_HEAT: "heat",
    MODE_GAS_HEAT: "heat",
}

# Default device mode when HA selects a generic mode (preset overrides for heat)
HA_TO_DEVICE_DEFAULT: dict[str, int] = {
    "off": MODE_OFF,
    "fan_only": MODE_FAN_ONLY,
    "cool": MODE_COOL,
    "heat": MODE_HEAT_PUMP,   # overridden by preset if configured
    "dry": MODE_DRY,
    "auto": MODE_AUTO,
}

# ── Heat-source preset modes ──────────────────────────────────────────────────
# Maps preset name → device mode number (used for HA preset_mode)
HEAT_TYPE_PRESETS: dict[str, int] = {
    "Heat Pump": MODE_HEAT_PUMP,
    "Furnace": MODE_FURNACE,
    "Heat Strip": MODE_HEAT_STRIP,
    "Electric Heat": MODE_ELECTRIC_HEAT,
    "Gas Heat": MODE_GAS_HEAT,
    "Heat": MODE_HEAT,
}
HEAT_TYPE_REVERSE: dict[int, str] = {v: k for k, v in HEAT_TYPE_PRESETS.items()}

# ── Fan speed values ──────────────────────────────────────────────────────────
FAN_OFF = 0
FAN_LOW = 1
FAN_HIGH = 2
FAN_CYCLED_LOW = 65
FAN_CYCLED_HIGH = 66
FAN_AUTO = 128

HA_FAN_TO_VALUE: dict[str, int] = {
    "off": FAN_OFF,
    "low": FAN_LOW,
    "high": FAN_HIGH,
    "auto": FAN_AUTO,
}

FAN_VALUE_TO_HA: dict[int, str] = {
    FAN_OFF: "off",
    FAN_LOW: "low",
    FAN_HIGH: "high",
    FAN_CYCLED_LOW: "low",
    FAN_CYCLED_HIGH: "high",
    FAN_AUTO: "auto",
}

# ── Z_sts array indices ───────────────────────────────────────────────────────
IDX_AUTO_HEAT_SP = 0
IDX_AUTO_COOL_SP = 1
IDX_COOL_SP = 2
IDX_HEAT_SP = 3
IDX_DRY_SP = 4
IDX_FAN_ONLY_SPEED = 6
IDX_COOL_FAN_SPEED = 7
IDX_ELECTRIC_FAN_SPEED = 8
IDX_AUTO_FAN_SPEED = 9
IDX_MODE = 10
IDX_GAS_FAN_SPEED = 11
IDX_AMBIENT_TEMP = 12
IDX_FAULT = 14
IDX_STATUS_FLAGS = 15

# STATUS_FLAGS bitmask
FLAG_CYCLE_ACTIVE = 0x01
FLAG_IS_COOLING = 0x02
FLAG_IS_HEATING = 0x04

# ── Timing ────────────────────────────────────────────────────────────────────
POLL_INTERVAL_S = 4.0               # Status poll interval (matches official app)
CONFIG_ZONE_DELAY_S = 0.3           # Delay between zone config requests
AUTH_STEP_DELAY_S = 0.2             # Delay before authenticating
INITIAL_STATUS_DELAY_S = 0.5        # Delay before first status request
DEBOUNCE_DELAY_S = 0.5              # Debounce for temperature / fan changes
STATUS_SUPPRESS_S = 4.0             # Suppress status updates after commands
READ_RESPONSE_DELAY_S = 0.1         # Delay before reading response after write
READ_RETRY_DELAY_S = 0.1            # Delay for additional reads when JSON incomplete
WRITE_INITIAL_DELAY_S = 0.1         # Delay before first write attempt
STALE_TIMEOUT_S = 300.0             # Watchdog stale-connection timeout

# Reconnect backoff
RECONNECT_BACKOFF_BASE_S = 5
RECONNECT_BACKOFF_CAP_S = 120

# ── Temperature defaults (°F) ─────────────────────────────────────────────────
DEFAULT_MIN_TEMP = 60
DEFAULT_MAX_TEMP = 90
TEMP_STEP = 1.0

# Fan field names for JSON commands (depend on heat mode type)
FAN_FIELD_ELECTRIC = "eleFan"
FAN_FIELD_GAS = "gasFan"
FAN_FIELD_COOL = "coolFan"
FAN_FIELD_AUTO = "autoFan"
FAN_FIELD_FAN_ONLY = "fanOnly"
