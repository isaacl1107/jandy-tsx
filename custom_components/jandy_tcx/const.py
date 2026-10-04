"""Constants for the Jandy AquaLink TCX integration."""

from __future__ import annotations

DOMAIN = "jandy_tcx"

# Public Zodiac / iAquaLink cloud endpoints used by the official app.
API_KEY = "EOOEMOW4YR6QNB07"
LOGIN_URL = "https://prod.zodiac-io.com/users/v1/login"
REFRESH_URL = "https://prod.zodiac-io.com/users/v1/refresh"
DEVICES_URL = "https://r-api.iaqualink.net/v2/devices.json"
SHADOW_URL = "https://prod.zodiac-io.com/devices/v2/{serial}/shadow"
WS_URL = "wss://prod-socket.zodiac-io.com/devices"
USER_AGENT = "iAqualink/934 CFNetwork/3826.500.131 Darwin/24.5.0"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_SERIAL = "serial"
CONF_MOCK = "mock"
CONF_POLL_INTERVAL = "poll_interval"
CONF_SCHEDULES = "schedules"

DEFAULT_POLL_INTERVAL = 30
DEFAULT_NAME = "Jandy TCX"

# Wire namespaces / actions (AquaLink TCX cloud protocol).
NAMESPACE_TCX = "tcx"
NAMESPACE_FILTRATION = "filtration"
NAMESPACE_AUTHORIZATION = "authorization"
SERVICE_AUTHORIZATION = "Authorization"
SERVICE_STATE_CONTROLLER = "StateController"

ACTION_SUBSCRIBE = "subscribe"
ACTION_SET_FILTER_PUMP_STATE = "setFilterPumpState"
ACTION_SET_HEAT_ENABLED = "setHeatEnabled"
ACTION_SET_WATER_TEMP_SETPOINT = "setWaterTempSetpoint"
ACTION_SET_AUX_STATE = "setAuxState"
ACTION_SET_AUX_LIGHT = "setAuxLight"
ACTION_SET_STATE = "setState"

# Temperatures on the wire are tenths of a degree Fahrenheit.
TEMP_SCALE = 10
TEMP_MIN_F = 60
TEMP_MAX_F = 104

ATTR_NEXT_ON = "next_on"
ATTR_NEXT_OFF = "next_off"
ATTR_SCHEDULE_ACTIVE = "schedule_active"

PLATFORMS = [
    "binary_sensor",
    "climate",
    "light",
    "number",
    "sensor",
    "switch",
]
