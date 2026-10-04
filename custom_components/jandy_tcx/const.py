"""Constants for the Jandy AquaLink TCX integration."""

from __future__ import annotations

DOMAIN = "jandy_tcx"

# Public Zodiac / iAquaLink cloud endpoints used by the official app.
# NB07 is the classic iAquaLink key; NB11 is used by some Zodiac/EU apps.
API_KEY = "EOOEMOW4YR6QNB07"
API_KEY_PROD = "EOOEMOW4YR6QNB11"
API_SIGNING_KEY = "cj7iYKjiKxOqiLcN65PffA"
LOGIN_URL = "https://prod.zodiac-io.com/users/v1/login"
REFRESH_URL = "https://prod.zodiac-io.com/users/v1/refresh"
DEVICES_URL = "https://r-api.iaqualink.net/v2/devices.json"
DEVICES_URL_LEGACY = "https://r-api.iaqualink.net/devices.json"
SHADOW_URL = "https://prod.zodiac-io.com/devices/v2/{serial}/shadow"
WS_URL = "wss://prod-socket.zodiac-io.com/devices"
USER_AGENT = "okhttp/3.14.7"

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
NAMESPACE_PIB = "pib"
NAMESPACE_ZIGBEE = "zigbee"
SERVICE_AUTHORIZATION = "Authorization"
SERVICE_STATE_CONTROLLER = "StateController"

ACTION_SUBSCRIBE = "subscribe"
ACTION_SET_FILTER_PUMP_STATE = "setFilterPumpState"
ACTION_SET_HEAT_ENABLED = "setHeatEnabled"
ACTION_SET_WATER_TEMP_SETPOINT = "setWaterTempSetpoint"
ACTION_SET_AUX_STATE = "setAuxState"
ACTION_SET_AUX_LIGHT = "setAuxLight"
ACTION_SET_ZIGBEE_STATE = "setZigbeeState"
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
