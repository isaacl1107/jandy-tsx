"""Zodiac / iAquaLink cloud client for AquaLink TCX controllers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp

from .const import (
    ACTION_SET_AUX_LIGHT,
    ACTION_SET_AUX_STATE,
    ACTION_SET_FILTER_PUMP_STATE,
    ACTION_SET_HEAT_ENABLED,
    ACTION_SET_STATE,
    ACTION_SET_WATER_TEMP_SETPOINT,
    ACTION_SET_ZIGBEE_STATE,
    ACTION_SUBSCRIBE,
    API_KEY,
    API_KEY_PROD,
    API_SIGNING_KEY,
    DEVICES_URL,
    DEVICES_URL_LEGACY,
    LOGIN_URL,
    NAMESPACE_AUTHORIZATION,
    NAMESPACE_FILTRATION,
    NAMESPACE_PIB,
    NAMESPACE_TCX,
    NAMESPACE_ZIGBEE,
    REFRESH_URL,
    SERVICE_AUTHORIZATION,
    SERVICE_STATE_CONTROLLER,
    SHADOW_URL,
    TEMP_SCALE,
    USER_AGENT,
    USER_AGENT_MOBILE,
    WS_URL,
)

# Namespace keys that identify a TCX Authorization full-state payload.
_AUTH_NAMESPACE_KEYS = frozenset(
    {"main", "filt", "ecm", "pib0", "zig", "fea", "sched", "scene"}
)

_LOGGER = logging.getLogger(__name__)


class TcxAuthError(Exception):
    """Raised when Zodiac authentication fails."""


class TcxApiError(Exception):
    """Raised for non-auth transport / API failures."""


@dataclass
class TcxState:
    """Normalized TCX controller state used by Home Assistant entities."""

    serial: str = ""
    name: str = "Pool"
    online: bool = False
    water_temp_f: float | None = None
    air_temp_f: float | None = None
    heater_setpoint_f: float | None = None
    heater_enabled: bool = False
    heater_running: bool = False
    heater_name: str = "Pool Heater"
    temp_unit_celsius: bool = False
    pump_on: bool = False
    pump_rpm: int | None = None
    pump_min_rpm: int = 1000
    pump_max_rpm: int = 3450
    light_on: bool = False
    light_color: int = 0
    light_name: str = "Pool Light"
    light_key: str | None = None
    light_is_color: bool = False
    light_available: bool = False
    water_feature_on: bool = False
    water_feature_name: str = "Water feature"
    water_feature_key: str | None = None
    water_feature_available: bool = False
    swc_percent: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "name": self.name,
            "online": self.online,
            "water_temp_f": self.water_temp_f,
            "air_temp_f": self.air_temp_f,
            "heater_setpoint_f": self.heater_setpoint_f,
            "heater_enabled": self.heater_enabled,
            "heater_running": self.heater_running,
            "heater_name": self.heater_name,
            "temp_unit_celsius": self.temp_unit_celsius,
            "pump_on": self.pump_on,
            "pump_rpm": self.pump_rpm,
            "pump_min_rpm": self.pump_min_rpm,
            "pump_max_rpm": self.pump_max_rpm,
            "light_on": self.light_on,
            "light_color": self.light_color,
            "light_name": self.light_name,
            "light_key": self.light_key,
            "light_is_color": self.light_is_color,
            "light_available": self.light_available,
            "water_feature_on": self.water_feature_on,
            "water_feature_name": self.water_feature_name,
            "water_feature_key": self.water_feature_key,
            "water_feature_available": self.water_feature_available,
            "swc_percent": self.swc_percent,
        }


def _temp_unit_is_celsius(reported: dict[str, Any]) -> bool:
    """tempSetting: 0 = °C, 1 = °F (Zodiac shadow)."""
    setting = reported.get("tempSetting")
    try:
        return int(setting) == 0
    except (TypeError, ValueError):
        return False


def _wire_temp_to_f(value: Any, *, celsius: bool) -> float | None:
    """Convert a wire temperature (tenths of the active unit) to °F."""
    if value is None:
        return None
    try:
        display = float(value) / TEMP_SCALE
    except (TypeError, ValueError):
        return None
    if celsius:
        return round(display * 9.0 / 5.0 + 32.0, 1)
    return round(display, 1)


def _f_to_wire_temp(temp_f: float, *, celsius: bool) -> int:
    """Convert °F to wire tenths in the controller's active unit."""
    if celsius:
        celsius_val = (float(temp_f) - 32.0) * 5.0 / 9.0
        return int(round(celsius_val * TEMP_SCALE))
    return int(round(float(temp_f) * TEMP_SCALE))


def _tenths_to_f(value: Any) -> float | None:
    """Deprecated helper — assumes Fahrenheit tenths. Prefer _wire_temp_to_f."""
    return _wire_temp_to_f(value, celsius=False)


def _f_to_tenths(temp_f: float) -> int:
    """Deprecated helper — assumes Fahrenheit tenths. Prefer _f_to_wire_temp."""
    return _f_to_wire_temp(temp_f, celsius=False)


def _merge_reported(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge Authorization namespace-keyed payload or flat reported state."""
    desired, reported = _extract_desired_reported(payload)
    if reported:
        return dict(reported)
    if desired:
        # Some streamer acks only carry desired; treat as a delta hint.
        return dict(desired)

    merged: dict[str, Any] = {}
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        _desired, _reported = _extract_desired_reported(value)
        if _reported:
            merged.update(_reported)
        elif _desired:
            merged.update(_desired)
        elif "metadata" not in value and any(
            key in value for key in ("water", "filt0", "TspBdy0", "pool", "lvh1")
        ):
            merged.update(value)
    return merged


def _extract_desired_reported(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (desired, reported) dicts from a WS/REST shadow-shaped payload."""
    state = payload.get("state")
    if not isinstance(state, dict):
        return {}, {}
    desired = state.get("desired")
    reported = state.get("reported")
    return (
        desired if isinstance(desired, dict) else {},
        reported if isinstance(reported, dict) else {},
    )


def _summarize_aux(delta: dict[str, Any]) -> dict[str, Any]:
    """Compact aux*/st summary for logs."""
    out: dict[str, Any] = {}
    for key, value in delta.items():
        if key.startswith("aux") and isinstance(value, dict):
            out[key] = {
                "st": value.get("st"),
                "cmdClr": value.get("cmdClr"),
                "currClr": value.get("currClr"),
            }
    return out


def _deep_merge(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge delta into a copy of base (delta wins on leaves)."""
    out = dict(base)
    for key, value in delta.items():
        existing = out.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            out[key] = _deep_merge(existing, value)
        else:
            out[key] = value
    return out


def parse_reported(reported: dict[str, Any], *, serial: str = "") -> TcxState:
    """Parse a flat TCX reported tree into TcxState."""
    state = TcxState(serial=serial or str(reported.get("sn") or ""), raw=reported)
    state.name = str(reported.get("name") or reported.get("fr") or "Pool")
    aws = reported.get("aws") or {}
    state.online = str(aws.get("status", "")).lower() == "connected" or bool(
        reported
    )
    celsius = _temp_unit_is_celsius(reported)
    state.temp_unit_celsius = celsius

    water = reported.get("water") or {}
    if isinstance(water, dict) and "value" in water:
        state.water_temp_f = _wire_temp_to_f(water.get("value"), celsius=celsius)
    elif "waterTemp" in reported:
        state.water_temp_f = _wire_temp_to_f(
            reported.get("waterTemp"), celsius=celsius
        )

    air = reported.get("air") or {}
    if isinstance(air, dict) and "value" in air:
        state.air_temp_f = _wire_temp_to_f(air.get("value"), celsius=celsius)
    elif "airTemp" in reported:
        # Some payloads publish airTemp already in whole degrees of the
        # active unit (not tenths). Values > 200 are treated as tenths.
        try:
            air_raw = float(reported["airTemp"])
            if air_raw > 200:
                state.air_temp_f = _wire_temp_to_f(air_raw, celsius=celsius)
            elif celsius:
                state.air_temp_f = round(air_raw * 9.0 / 5.0 + 32.0, 1)
            else:
                state.air_temp_f = air_raw
        except (TypeError, ValueError):
            state.air_temp_f = None

    tsp = reported.get("TspBdy0") or {}
    if isinstance(tsp, dict):
        state.heater_setpoint_f = _wire_temp_to_f(
            tsp.get("waterTempSet"), celsius=celsius
        )
        state.heater_enabled = bool(tsp.get("heatEnabled"))
        if tsp.get("name"):
            state.heater_name = str(tsp["name"]) + " Heater"

    lvh = reported.get("lvh1") or {}
    if isinstance(lvh, dict):
        # lvh1.en: 0/≥7 off, 1–5 standby, 6 heating.
        try:
            lvh_en = int(lvh.get("en") or 0)
        except (TypeError, ValueError):
            lvh_en = 0
        state.heater_running = lvh_en == 6
        if lvh.get("fr"):
            state.heater_name = str(lvh["fr"])

    filt = reported.get("filt0") or {}
    pool = reported.get("pool") or {}
    ecm = reported.get("ecm0") or {}
    # Any of these st=1 means the filtration path is running. Prefer ecm0
    # (motor) when present — REST main shadow often omits filt0/pool.
    pump_flags: list[bool] = []
    for obj in (ecm, filt, pool):
        if isinstance(obj, dict) and "st" in obj:
            try:
                pump_flags.append(int(obj.get("st") or 0) == 1)
            except (TypeError, ValueError):
                pass
    if pump_flags:
        state.pump_on = any(pump_flags)

    if isinstance(ecm, dict):
        for key in ("cmdSpd", "reqSpd", "sp"):
            if ecm.get(key) is not None:
                try:
                    state.pump_rpm = int(ecm[key])
                    break
                except (TypeError, ValueError):
                    pass
        try:
            state.pump_min_rpm = int(ecm.get("minSpd") or state.pump_min_rpm)
            state.pump_max_rpm = int(ecm.get("maxSpd") or state.pump_max_rpm)
        except (TypeError, ValueError):
            pass
    elif isinstance(filt, dict):
        for key in ("sp", "manSpd"):
            if filt.get(key) is not None:
                try:
                    state.pump_rpm = int(filt[key])
                    break
                except (TypeError, ValueError):
                    pass
        try:
            state.pump_min_rpm = int(filt.get("minSpd") or state.pump_min_rpm)
            state.pump_max_rpm = int(filt.get("maxSpd") or state.pump_max_rpm)
        except (TypeError, ValueError):
            pass

    # Aux relays (auxN) and Zigbee aux (auxzN): water feature + pool light.
    # Color-capable et values: JL / IB / PSS / HU (see TCX LightType).
    light_found = False
    for key, value in reported.items():
        if not key.startswith("aux") or not isinstance(value, dict):
            continue
        app = str(value.get("app") or "")
        et = str(value.get("et") or "")
        fr = str(value.get("fr") or "").lower()
        if app == "WF" or "waterfall" in fr or "water feature" in fr:
            state.water_feature_available = True
            state.water_feature_key = key
            state.water_feature_on = int(value.get("st") or 0) == 1
            state.water_feature_name = str(value.get("fr") or "Water feature")
            continue
        try:
            ty = int(value.get("ty")) if value.get("ty") is not None else None
        except (TypeError, ValueError):
            ty = None
        is_color = app in {"POOL_LT", "POOL_LIGHT"} or et in {
            "JL",
            "IB",
            "PSS",
            "HU",
        }
        # ty: 2=white light, 6=pool light (AuxType); et WL=white light.
        is_light = (
            is_color
            or et == "WL"
            or ty in {2, 6}
            or "light" in fr
            or "lamp" in fr
        )
        if is_light:
            # Prefer wired aux color lights over Zigbee / name-only matches.
            if light_found:
                if state.light_is_color and not key.startswith("auxz"):
                    continue
                if state.light_is_color and not is_color:
                    continue
                if not key.startswith("auxz") and state.light_key and state.light_key.startswith("auxz"):
                    pass  # upgrade zigbee → wired
                elif state.light_key and not state.light_key.startswith("auxz"):
                    continue
            state.light_key = key
            state.light_on = int(value.get("st") or 0) == 1
            state.light_color = int(value.get("currClr") or value.get("cmdClr") or 0)
            state.light_name = str(value.get("fr") or "Pool Light")
            state.light_is_color = is_color or key.startswith("auxz")
            state.light_available = True
            light_found = True

    # Fallback: many TCX installs put the pool light on aux1 even when app/et
    # labels are blank in a partial REST shadow.
    if not light_found:
        for key in ("aux1", "aux2", "aux0", "auxz0"):
            value = reported.get(key)
            if not isinstance(value, dict):
                continue
            app = str(value.get("app") or "")
            fr = str(value.get("fr") or "").lower()
            if app == "WF" or "waterfall" in fr or "water feature" in fr:
                continue
            if "st" not in value:
                continue
            state.light_key = key
            state.light_on = int(value.get("st") or 0) == 1
            state.light_color = int(value.get("currClr") or value.get("cmdClr") or 0)
            state.light_name = str(value.get("fr") or "Pool Light")
            state.light_is_color = bool(
                value.get("currClr") is not None or value.get("cmdClr") is not None
            )
            state.light_available = True
            break

    swc = reported.get("swc0") or {}
    if isinstance(swc, dict):
        for key in ("outputPcnt", "stdPoolPcnt", "swc"):
            if swc.get(key) is not None:
                try:
                    state.swc_percent = int(swc[key])
                    break
                except (TypeError, ValueError):
                    pass

    return state


def mock_reported(serial: str = "MOCKTCX01") -> dict[str, Any]:
    """Synthetic reported tree for demo / offline setup."""
    return {
        "sn": serial,
        "model": "TCX-1",
        "deviceType": "tcx",
        "name": "Backyard Pool",
        "aws": {"status": "connected", "timestamp": int(time.time())},
        "tempSetting": 1,  # °F
        "airTemp": 74,
        "water": {
            "value": 780,
            "us": 1,
            "fr": "Pool Water",
            "en": 1,
        },
        "filt0": {
            "sp": 2400,
            "st": 1,
            "en": 1,
            "mn": 1000,
            "mx": 3450,
            "ap": "FILTER",
            "fr": "Filter Pump",
        },
        "pool": {"st": 1, "en": 1, "ap": "POOL_M", "fr": "Pool"},
        "ecm0": {
            "cmdSpd": 2400,
            "reqSpd": 2400,
            "minSpd": 1000,
            "maxSpd": 3450,
            "st": 1,
            "en": 1,
            "fr": "Variable Speed Pump",
        },
        "aux0": {
            "st": 0,
            "en": 1,
            "app": "WF",
            "et": "WL",
            "fr": "Water feature",
            "ty": 4,
        },
        "aux1": {
            "st": 0,
            "en": 1,
            "app": "POOL_LT",
            "et": "JL",
            "fr": "Pool Light",
            "currClr": 3,
            "cmdClr": 3,
            "ty": 6,
        },
        "TspBdy0": {
            "waterTempSet": 840,
            "solarTempSet": 900,
            "heatEnabled": False,
            "heatAvailable": 1,
            "gasEn": True,
            "status": 0,
            "value": 78,
            "name": "Pool",
        },
        "lvh1": {
            "en": 0,
            "app": "HEAT",
            "st": 0,
            "et": "GAS",
            "fr": "Pool Heater",
            "model": "LXi 400",
        },
        "swc0": {"swc": 50, "boost": 0},
    }


class TcxClient:
    """Async Zodiac cloud client with REST bootstrap + WebSocket commands."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        serial: str | None = None,
        *,
        mock: bool = False,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self.serial = serial or ""
        self.mock = mock
        self._id_token: str | None = None
        self._auth_token: str | None = None
        self._refresh_token: str | None = None
        self._user_id: str | None = None
        self._app_client_id: str | None = None
        self._token_expiry = 0.0
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_task: asyncio.Task | None = None
        self._ws_lock = asyncio.Lock()
        self._ws_auth_event = asyncio.Event()
        self._ws_debug_frames: list[dict[str, Any]] = []
        self._session_client_token: str | None = None
        self._reported: dict[str, Any] = {}
        self._desired: dict[str, Any] = {}
        self._listeners: list[Callable[[TcxState], None]] = []
        self._mock_state = mock_reported(serial or "MOCKTCX01")

    def add_listener(self, callback: Callable[[TcxState], None]) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        state = self.get_state()
        for callback in list(self._listeners):
            try:
                callback(state)
            except Exception:  # noqa: BLE001 - never break the socket loop
                _LOGGER.exception("Listener failed")

    def get_state(self) -> TcxState:
        source = self._mock_state if self.mock else self._reported
        return parse_reported(source, serial=self.serial)

    async def async_login(self) -> dict[str, Any]:
        if self.mock:
            self.serial = self.serial or "MOCKTCX01"
            self._reported = dict(self._mock_state)
            return {"mock": True, "id": "0"}

        email = (self._email or "").strip()
        password = self._password or ""
        if not email or not password:
            raise TcxAuthError("Email and password are required")

        # Vendors/clients disagree on the key field name and which api key to
        # send. Try the combinations known to work across iAquaLink apps.
        attempts: list[tuple[str, str]] = [
            ("api_key", API_KEY),
            ("api_key", API_KEY_PROD),
            ("apiKey", API_KEY),
            ("apikey", API_KEY),
            ("apiKey", API_KEY_PROD),
            ("apikey", API_KEY_PROD),
        ]
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        timeout = aiohttp.ClientTimeout(total=30)
        last_status: int | None = None
        last_body: Any = None

        for key_field, api_key in attempts:
            payload = {
                key_field: api_key,
                "email": email,
                "password": password,
            }
            try:
                async with self._session.post(
                    LOGIN_URL, json=payload, headers=headers, timeout=timeout
                ) as resp:
                    last_status = resp.status
                    try:
                        body = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                        body = await resp.text()
                    last_body = body
                    if resp.status in (401, 403):
                        _LOGGER.debug(
                            "Login attempt %s/…%s rejected (%s)",
                            key_field,
                            api_key[-4:],
                            resp.status,
                        )
                        continue
                    if resp.status >= 400:
                        _LOGGER.debug(
                            "Login attempt %s/…%s failed (%s): %s",
                            key_field,
                            api_key[-4:],
                            resp.status,
                            body,
                        )
                        continue
            except (aiohttp.ClientError, TimeoutError) as err:
                raise TcxApiError(f"Login transport error: {err}") from err

            if not isinstance(body, dict):
                continue

            oauth = body.get("userPoolOAuth") or {}
            token = oauth.get("IdToken")
            auth_token = body.get("authentication_token")
            user_id = body.get("id")
            if not token or not auth_token or user_id is None:
                _LOGGER.debug(
                    "Login response missing auth fields (keys=%s)",
                    list(body.keys()),
                )
                continue

            self._id_token = token
            self._auth_token = auth_token
            self._user_id = str(user_id)
            self._refresh_token = oauth.get("RefreshToken")
            cognito = body.get("cognitoPool") or {}
            self._app_client_id = (
                (cognito.get("appClientId") if isinstance(cognito, dict) else None)
                or oauth.get("appClientId")
                or body.get("appClientId")
                or oauth.get("ClientId")
                or oauth.get("client_id")
            )
            # Seed the WS clientToken the official apps send on every command.
            # Without cognitoPool.appClientId we used to invent a random 3-part
            # token and the cloud accepted Authorization but ignored writes.
            self._session_client_token = None
            if self._user_id and self._auth_token and self._app_client_id:
                self._session_client_token = (
                    f"{self._user_id}|{self._auth_token}|{self._app_client_id}"
                )
            try:
                expires_in = int(oauth.get("ExpiresIn", 3600))
            except (TypeError, ValueError):
                expires_in = 3600
            self._token_expiry = time.monotonic() + max(60, expires_in - 300)
            _LOGGER.info(
                "Zodiac login OK with %s/…%s for %s (app_client_id=%s auth_token=%s)",
                key_field,
                api_key[-4:],
                email,
                "yes" if self._app_client_id else "no",
                "yes" if self._auth_token else "no",
            )
            return body

        _LOGGER.warning(
            "Zodiac login failed for %s (last_status=%s body=%s)",
            email,
            last_status,
            last_body if not isinstance(last_body, dict) else list(last_body.keys()),
        )
        raise TcxAuthError(
            f"Login rejected (status={last_status}). "
            "Use the same email/password as the iAquaLink mobile app."
        )

    async def _ensure_token(self) -> str:
        if self.mock:
            return "mock"
        if self._id_token is None or time.monotonic() >= self._token_expiry:
            if self._refresh_token:
                try:
                    await self._async_refresh()
                except (TcxAuthError, TcxApiError):
                    await self.async_login()
            else:
                await self.async_login()
        assert self._id_token is not None
        return self._id_token

    async def _async_refresh(self) -> None:
        payload = {"email": self._email, "refresh_token": self._refresh_token}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        async with self._session.post(
            REFRESH_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status in (401, 403):
                raise TcxAuthError("Refresh rejected")
            if resp.status >= 400:
                raise TcxApiError(f"Refresh failed ({resp.status})")
        oauth = body.get("userPoolOAuth") or {}
        token = oauth.get("IdToken")
        if not token:
            raise TcxAuthError("Refresh response missing IdToken")
        self._id_token = token
        self._auth_token = body.get("authentication_token") or self._auth_token
        try:
            expires_in = int(oauth.get("ExpiresIn", 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        self._token_expiry = time.monotonic() + max(60, expires_in - 300)

    @staticmethod
    def _sign(parts: list[str]) -> str:
        message = ",".join(parts)
        return hmac.new(
            API_SIGNING_KEY.encode(), message.encode(), hashlib.sha1
        ).hexdigest()

    async def _async_fetch_devices_signed(self) -> list[dict[str, Any]]:
        """Modern iAquaLink device list (signed + Bearer IdToken)."""
        await self._ensure_token()
        timestamp = str(int(time.time()))
        signature = self._sign([str(self._user_id), timestamp])
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "api_key": API_KEY,
            "Authorization": f"Bearer {self._id_token}",
        }
        params = {
            "user_id": self._user_id,
            "signature": signature,
            "timestamp": timestamp,
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with self._session.get(
            DEVICES_URL, params=params, headers=headers, timeout=timeout
        ) as resp:
            text = await resp.text()
            if resp.status in (401, 403):
                raise TcxAuthError(f"Device list unauthorized ({resp.status})")
            if resp.status >= 400:
                raise TcxApiError(f"Device list failed ({resp.status}): {text[:200]}")
            try:
                body = json.loads(text)
            except json.JSONDecodeError as err:
                raise TcxApiError("Device list returned non-JSON") from err
        if isinstance(body, list):
            return [item for item in body if isinstance(item, dict)]
        if isinstance(body, dict):
            devices = body.get("devices", [])
            if isinstance(devices, list):
                return [item for item in devices if isinstance(item, dict)]
        return []

    async def _async_fetch_devices_legacy(self) -> list[dict[str, Any]]:
        """Legacy devices.json using authentication_token query params."""
        await self._ensure_token()
        params = {
            "api_key": API_KEY,
            "authentication_token": self._auth_token,
            "user_id": self._user_id,
        }
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": self._id_token or "",
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with self._session.get(
            DEVICES_URL_LEGACY, params=params, headers=headers, timeout=timeout
        ) as resp:
            text = await resp.text()
            if resp.status in (401, 403):
                raise TcxAuthError(f"Legacy device list unauthorized ({resp.status})")
            if resp.status >= 400:
                raise TcxApiError(
                    f"Legacy device list failed ({resp.status}): {text[:200]}"
                )
            try:
                body = json.loads(text)
            except json.JSONDecodeError as err:
                raise TcxApiError("Legacy device list returned non-JSON") from err
        if isinstance(body, list):
            return [item for item in body if isinstance(item, dict)]
        if isinstance(body, dict):
            devices = body.get("devices", [])
            if isinstance(devices, list):
                return [item for item in devices if isinstance(item, dict)]
        return []

    async def async_list_devices(self) -> list[dict[str, Any]]:
        """Return all devices on the account."""
        if self.mock:
            return [
                {
                    "serial_number": self.serial or "MOCKTCX01",
                    "name": "Backyard Pool",
                    "device_type": "tcx",
                }
            ]

        try:
            return await self._async_fetch_devices_signed()
        except (TcxAuthError, TcxApiError) as err:
            _LOGGER.warning("Signed device list failed (%s); trying legacy", err)
            return await self._async_fetch_devices_legacy()

    async def async_list_tcx_devices(self) -> list[dict[str, Any]]:
        devices = await self.async_list_devices()
        tcx = [
            device
            for device in devices
            if isinstance(device, dict)
            and str(device.get("device_type", "")).lower() == "tcx"
            and device.get("serial_number")
        ]
        if not tcx and devices:
            types = sorted(
                {
                    str(device.get("device_type"))
                    for device in devices
                    if isinstance(device, dict) and device.get("device_type")
                }
            )
            _LOGGER.warning(
                "No TCX controllers found; account device types: %s", types
            )
        return tcx

    async def async_get_shadow(self) -> dict[str, Any]:
        if self.mock:
            self._reported = dict(self._mock_state)
            return {"state": {"reported": self._reported}}

        if not self.serial:
            raise TcxApiError("No TCX serial configured")
        token = await self._ensure_token()
        if not self._user_id:
            raise TcxApiError("Missing user id for shadow signature")

        # TCX shadow GET requires signature = HMAC-SHA1(serial.upper,user_id).
        url = SHADOW_URL.format(serial=self.serial)
        params = {
            "signature": self._sign(
                [self.serial.upper(), str(self._user_id)]
            )
        }
        headers = {
            "Authorization": token,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        timeout = aiohttp.ClientTimeout(total=30)

        async def _do_get(auth_token: str) -> tuple[int, Any]:
            req_headers = {**headers, "Authorization": auth_token}
            async with self._session.get(
                url, params=params, headers=req_headers, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    return resp.status, await resp.text()
                return resp.status, await resp.json(content_type=None)

        status, body = await _do_get(token)
        if status in (401, 403):
            self._id_token = None
            token = await self._ensure_token()
            status, body = await _do_get(token)
        if status >= 400:
            raise TcxApiError(f"Shadow GET failed ({status}): {body}")

        reported = (body.get("state") or {}).get("reported") or {}
        if isinstance(reported, dict) and reported:
            # Merge — never wipe WS-only keys (aux from pib0, auxz from zig, ecm…).
            self._reported = _deep_merge(self._reported, reported)
            self._notify()
        # REST sub-shadow GETs return 401 on real hardware; aux/lights come
        # exclusively from the websocket Authorization full-state push.
        return body

    def _client_token(self) -> str:
        # Prefer a token echoed by the cloud (official apps reuse it).
        if self._session_client_token:
            return self._session_client_token
        if self._user_id and self._auth_token and self._app_client_id:
            return f"{self._user_id}|{self._auth_token}|{self._app_client_id}"
        if self._user_id:
            # iaqualink-py fallback when Cognito appClientId is missing:
            # two-part userId|<random>, not a fabricated 3-part token.
            return f"{self._user_id}|{uuid.uuid4().hex}"
        return f"{uuid.uuid4().hex}|{uuid.uuid4().hex}"

    @staticmethod
    def _find_client_token(value: Any) -> str | None:
        if isinstance(value, dict):
            token = value.get("clientToken")
            if isinstance(token, str) and token.strip():
                return token.strip()
            for item in value.values():
                found = TcxClient._find_client_token(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = TcxClient._find_client_token(item)
                if found:
                    return found
        return None

    async def _ws_close_quiet(self) -> None:
        if self._ws_task is not None and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self._ws_task = None
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None

    async def _ws_read_until_auth(self, *, timeout: float) -> bool:
        """Read websocket messages inline until Authorization arrives."""
        assert self._ws is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._ws_auth_event.is_set():
            remaining = max(0.1, deadline - time.monotonic())
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
            except TimeoutError:
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    _LOGGER.debug("TCX WS non-JSON text: %s", msg.data[:200])
                    continue
                await self._handle_ws_frame(frame)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                _LOGGER.debug("TCX WS binary frame (%d bytes)", len(msg.data or b""))
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                _LOGGER.warning(
                    "TCX WS closed during auth wait (type=%s close=%s)",
                    msg.type,
                    self._ws.close_code if self._ws else None,
                )
                break
        return self._ws_auth_event.is_set()

    async def async_connect_ws(self, *, wait_for_auth: bool = True) -> None:
        if self.mock:
            return
        async with self._ws_lock:
            if self._ws and not self._ws.closed and self._ws_auth_event.is_set():
                if self._ws_task is None or self._ws_task.done():
                    self._ws_task = asyncio.create_task(self._ws_receive_loop())
                return
            if self._ws and not self._ws.closed and not wait_for_auth:
                if self._ws_task is None or self._ws_task.done():
                    self._ws_task = asyncio.create_task(self._ws_receive_loop())
                return

            await self._ensure_token()
            agents = (USER_AGENT_MOBILE, USER_AGENT, USER_AGENT_MOBILE)
            last_error: Exception | None = None

            for attempt, agent in enumerate(agents, start=1):
                await self._ws_close_quiet()
                self._ws_auth_event.clear()
                self._ws_debug_frames.clear()
                try:
                    # Use the HA aiohttp session SSL context — never call
                    # ssl.create_default_context() on the event loop.
                    headers = {
                        "Authorization": self._id_token or "",
                        "User-Agent": agent,
                    }
                    self._ws = await self._session.ws_connect(
                        WS_URL,
                        headers=headers,
                        heartbeat=20,
                        autoping=True,
                        max_msg_size=0,
                    )
                    subscribe = {
                        "action": ACTION_SUBSCRIBE,
                        "version": 1,
                        "namespace": NAMESPACE_AUTHORIZATION,
                        "service": SERVICE_AUTHORIZATION,
                        "payload": {"userId": int(self._user_id or 0)},
                        "target": self.serial,
                    }
                    await self._ws.send_json(subscribe)
                    _LOGGER.info(
                        "TCX WS subscribe sent (attempt %d/%d ua=%s)",
                        attempt,
                        len(agents),
                        agent.split("/")[0],
                    )

                    if wait_for_auth:
                        ok = await self._ws_read_until_auth(timeout=10.0)
                        if ok:
                            break
                        _LOGGER.warning(
                            "TCX WS auth not ready on attempt %d "
                            "(%d frames, close=%s)",
                            attempt,
                            len(self._ws_debug_frames),
                            self._ws.close_code if self._ws else None,
                        )
                        # Resubscribe once on the same socket if still open.
                        if self._ws and not self._ws.closed:
                            await self._ws.send_json(subscribe)
                            if await self._ws_read_until_auth(timeout=8.0):
                                break
                    else:
                        break
                except Exception as err:  # noqa: BLE001
                    last_error = err
                    _LOGGER.warning("TCX WS connect attempt %d failed: %s", attempt, err)
            else:
                if wait_for_auth and not self._ws_auth_event.is_set():
                    _LOGGER.error(
                        "TCX websocket Authorization never arrived after retries "
                        "(%d frames). Light/aux control will not work until the "
                        "next successful websocket session.",
                        len(self._ws_debug_frames),
                    )
                    if last_error is not None:
                        raise TcxApiError(
                            f"Websocket Authorization failed: {last_error}"
                        ) from last_error

            # Hand off ongoing reads to the background loop.
            if self._ws and not self._ws.closed:
                if self._ws_task is None or self._ws_task.done():
                    self._ws_task = asyncio.create_task(self._ws_receive_loop())

    async def _ws_receive_loop(self) -> None:
        assert self._ws is not None
        _LOGGER.debug("TCX WS receive loop started")
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        frame = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_ws_frame(frame)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    _LOGGER.warning(
                        "TCX WS receive loop ending (type=%s close=%s err=%s)",
                        msg.type,
                        self._ws.close_code,
                        self._ws.exception(),
                    )
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("TCX websocket receive loop failed")
        finally:
            _LOGGER.warning("TCX websocket closed")

    async def _handle_ws_frame(self, frame: dict[str, Any]) -> None:
        token = self._find_client_token(frame)
        if token and token != self._session_client_token:
            self._session_client_token = token
            _LOGGER.debug(
                "Captured WS clientToken (%d parts)",
                token.count("|") + 1,
            )

        payload = frame.get("payload")
        if not isinstance(payload, dict):
            if len(self._ws_debug_frames) < 40:
                self._ws_debug_frames.append(
                    {
                        "service": frame.get("service"),
                        "namespace": frame.get("namespace"),
                        "action": frame.get("action"),
                        "payload": type(frame.get("payload")).__name__,
                    }
                )
            return

        service = str(frame.get("service") or "")
        if service == "ErrorStreamer" or payload.get("error"):
            _LOGGER.warning("TCX WS error frame: %s", frame)

        # Flat StateStreamer/DataStreamer shadow deltas carry both desired and
        # reported. Older code only kept reported when it was non-empty, which
        # dropped command acks that only appeared under desired.
        desired, reported = _extract_desired_reported(payload)
        # Namespace-keyed deltas (same shape as Authorization) also appear on
        # some streamer pushes — merge those trees too.
        if not desired and not reported:
            for value in payload.values():
                if not isinstance(value, dict):
                    continue
                d_part, r_part = _extract_desired_reported(value)
                if d_part:
                    desired = _deep_merge(desired, d_part)
                if r_part:
                    reported = _deep_merge(reported, r_part)

        if len(self._ws_debug_frames) < 40:
            sample = {
                "service": frame.get("service"),
                "namespace": frame.get("namespace"),
                "action": frame.get("action"),
                "event": frame.get("event"),
                "desired_aux": _summarize_aux(desired),
                "reported_aux": _summarize_aux(reported),
                "payload_keys": sorted(payload.keys()),
            }
            self._ws_debug_frames.append(sample)
            _LOGGER.debug("TCX WS frame: %s", sample)

        if service in {"StateStreamer", "DataStreamer", "EventStreamer"} or (
            desired or reported
        ):
            if desired or reported:
                _LOGGER.info(
                    "TCX %s delta desired_aux=%s reported_aux=%s",
                    service or "shadow",
                    _summarize_aux(desired) or desired,
                    _summarize_aux(reported) or {
                        key: reported.get(key)
                        for key in list(reported)[:8]
                    },
                )
            elif service in {"StateStreamer", "DataStreamer", "EventStreamer"}:
                _LOGGER.info(
                    "TCX %s empty desired/reported (command likely rejected)",
                    service,
                )
            if desired:
                # AWS IoT clears accepted desired keys by pushing null; drop those.
                clean_desired = {
                    key: value
                    for key, value in desired.items()
                    if value is not None
                }
                if clean_desired:
                    self._desired = _deep_merge(self._desired, clean_desired)
                for key, value in desired.items():
                    if value is None:
                        self._desired.pop(key, None)
            if reported:
                self._reported = _deep_merge(self._reported, reported)
                self._notify()
            elif desired:
                # Cloud accepted desired but device has not reported yet.
                self._notify()
            # Continue — Authorization frames also use namespace-keyed shape below.

        merged = _merge_reported(payload)
        if not merged and not desired and not reported:
            return

        looks_like_auth = service == SERVICE_AUTHORIZATION or bool(
            _AUTH_NAMESPACE_KEYS.intersection(payload)
        )
        if looks_like_auth and merged:
            self._reported = _deep_merge(self._reported, merged)
            if (
                any(key.startswith("aux") for key in merged)
                or "water" in merged
                or "filt0" in merged
                or "ecm0" in merged
            ):
                self._ws_auth_event.set()
            self._notify()
        elif merged and service not in {
            "StateStreamer",
            "DataStreamer",
            "EventStreamer",
        }:
            self._reported = _deep_merge(self._reported, merged)
            self._notify()

    async def _send_command(
        self,
        *,
        namespace: str,
        action: str,
        delta: dict[str, Any],
        optimistic: bool = True,
    ) -> None:
        if self.mock:
            self._apply_mock_delta(delta)
            self._notify()
            return

        await self.async_connect_ws()
        if self._ws is None or self._ws.closed:
            raise TcxApiError("WebSocket unavailable for command")
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._ws_receive_loop())
            # Let the receive loop attach before we send.
            await asyncio.sleep(0)

        # Live hardware (and robot/cyclonext examples) require the MQTT-style
        # state.desired wrapper. Flat deltas with a valid clientToken still
        # produced empty StateStreamer acks and never flipped aux.st.
        if "state" in delta and isinstance(delta.get("state"), dict):
            payload_body = dict(delta)
        else:
            payload_body = {"state": {"desired": delta}}
        frame = {
            "version": 1,
            "action": action,
            "namespace": namespace,
            "service": SERVICE_STATE_CONTROLLER,
            "target": self.serial,
            "payload": {**payload_body, "clientToken": self._client_token()},
        }
        _LOGGER.info(
            "TCX WS command %s/%s keys=%s optimistic=%s",
            namespace,
            action,
            list(delta.keys()),
            optimistic,
        )
        await self._ws.send_json(frame)
        if optimistic:
            # Deep-merge only — never replace an aux object with a bare {st: N}
            # stub (that wipes app/et/fr and breaks discovery).
            merge_delta = delta
            if "state" in delta and isinstance(delta.get("state"), dict):
                merge_delta = (delta["state"].get("desired") or {})
                if not isinstance(merge_delta, dict):
                    merge_delta = {}
            self._reported = _deep_merge(self._reported, merge_delta)
            self._notify()

    def _apply_mock_delta(self, delta: dict[str, Any]) -> None:
        for key, value in delta.items():
            if key == "pool" and isinstance(value, dict) and "st" in value:
                self._mock_state.setdefault("pool", {}).update(value)
                self._mock_state.setdefault("filt0", {})["st"] = value["st"]
                self._mock_state.setdefault("ecm0", {})["st"] = value["st"]
            elif key == "TspBdy0" and isinstance(value, dict):
                body = self._mock_state.setdefault("TspBdy0", {})
                body.update(value)
                if "heatEnabled" in value:
                    self._mock_state.setdefault("lvh1", {})["en"] = (
                        1 if value["heatEnabled"] else 0
                    )
            elif key.startswith("aux") and isinstance(value, dict):
                self._mock_state.setdefault(key, {}).update(value)
            elif key == "ecm0" and isinstance(value, dict):
                self._mock_state.setdefault("ecm0", {}).update(value)
            else:
                existing = self._mock_state.get(key)
                if isinstance(existing, dict) and isinstance(value, dict):
                    existing.update(value)
                else:
                    self._mock_state[key] = value
        self._reported = dict(self._mock_state)

    def _wire_uses_celsius(self) -> bool:
        return _temp_unit_is_celsius(self._reported)

    async def async_set_filter_pump(self, on: bool) -> None:
        """Toggle filtration. Requires a remote pool/filt0/ecm0 st echo."""
        want_st = 1 if on else 0
        delta = {"pool": {"st": want_st}}
        if self.mock:
            await self._send_command(
                namespace=NAMESPACE_FILTRATION,
                action=ACTION_SET_FILTER_PUMP_STATE,
                delta=delta,
            )
            return

        frames_before = len(self._ws_debug_frames)
        await self._send_command(
            namespace=NAMESPACE_FILTRATION,
            action=ACTION_SET_FILTER_PUMP_STATE,
            delta=delta,
            optimistic=False,
        )
        if await self._async_wait_pump_remote(
            want_st, frames_before=frames_before, timeout=12.0
        ):
            # Keep local cache aligned with the confirmed remote state.
            self._reported = _deep_merge(
                self._reported,
                {
                    "pool": {"st": want_st},
                    "filt0": {"st": want_st},
                    "ecm0": {"st": want_st},
                },
            )
            self._notify()
            return
        raise TcxApiError(
            f"Filter pump command sent but controller never echoed st={want_st}"
        )

    async def async_set_heater_enabled(self, enabled: bool) -> None:
        await self._send_command(
            namespace=NAMESPACE_TCX,
            action=ACTION_SET_HEAT_ENABLED,
            delta={"TspBdy0": {"heatEnabled": bool(enabled)}},
        )

    async def async_set_heater_setpoint(self, temp_f: float) -> None:
        await self._send_command(
            namespace=NAMESPACE_TCX,
            action=ACTION_SET_WATER_TEMP_SETPOINT,
            delta={
                "TspBdy0": {
                    "waterTempSet": _f_to_wire_temp(
                        temp_f, celsius=self._wire_uses_celsius()
                    )
                }
            },
        )

    async def async_set_pump_rpm(self, rpm: int) -> None:
        await self._send_command(
            namespace=NAMESPACE_TCX,
            action=ACTION_SET_STATE,
            delta={"ecm0": {"cmdSpd": int(rpm)}},
        )

    async def _async_wait_aux_remote(
        self,
        aux_key: str,
        want_st: int,
        *,
        frames_before: int,
        timeout: float = 10.0,
    ) -> bool:
        """Wait for remote aux.st via reported (required for success)."""
        deadline = time.monotonic() + timeout
        saw_desired = False
        extended = False
        while time.monotonic() < deadline:
            got_frame = len(self._ws_debug_frames) > frames_before or self.mock
            reported = self._reported.get(aux_key)
            desired = self._desired.get(aux_key)
            if (
                isinstance(reported, dict)
                and int(reported.get("st") or 0) == want_st
                and got_frame
            ):
                return True
            if (
                isinstance(desired, dict)
                and int(desired.get("st") or 0) == want_st
                and got_frame
            ):
                saw_desired = True
                # Cloud accepted desired — give the panel more time to report.
                if not extended:
                    deadline = max(deadline, time.monotonic() + 6.0)
                    extended = True
            await asyncio.sleep(0.2)
        if saw_desired:
            _LOGGER.warning(
                "TCX aux %s desired st=%s accepted by cloud but reported "
                "never changed (device may have rejected or ignored it)",
                aux_key,
                want_st,
            )
        return False

    async def _async_wait_pump_remote(
        self,
        want_st: int,
        *,
        frames_before: int,
        timeout: float = 12.0,
    ) -> bool:
        """Wait for filtration echo on pool / filt0 / ecm0."""
        deadline = time.monotonic() + timeout
        saw_desired = False
        while time.monotonic() < deadline:
            got_frame = len(self._ws_debug_frames) > frames_before or self.mock
            for key in ("pool", "filt0", "ecm0"):
                reported = self._reported.get(key)
                if (
                    isinstance(reported, dict)
                    and int(reported.get("st") or 0) == want_st
                    and got_frame
                ):
                    return True
                desired = self._desired.get(key)
                if (
                    isinstance(desired, dict)
                    and int(desired.get("st") or 0) == want_st
                    and got_frame
                ):
                    saw_desired = True
            await asyncio.sleep(0.2)
        if saw_desired:
            _LOGGER.warning(
                "TCX pump desired st=%s accepted by cloud but reported "
                "never changed",
                want_st,
            )
        return False

    async def async_set_light(self, on: bool, color: int | None = None) -> None:
        """Toggle pool light, trying known TCX command envelopes until echoed."""
        state = self.get_state()
        aux_key = state.light_key
        if not aux_key:
            for key, value in state.raw.items():
                if key.startswith("aux") and isinstance(value, dict):
                    app = str(value.get("app") or "")
                    et = str(value.get("et") or "")
                    fr = str(value.get("fr") or "").lower()
                    try:
                        ty = (
                            int(value.get("ty"))
                            if value.get("ty") is not None
                            else None
                        )
                    except (TypeError, ValueError):
                        ty = None
                    if (
                        app in {"POOL_LT", "POOL_LIGHT"}
                        or et in {"JL", "IB", "PSS", "HU", "WL"}
                        or ty in {2, 6}
                        or "light" in fr
                        or "lamp" in fr
                    ):
                        aux_key = key
                        break
        if not aux_key:
            for key in ("aux1", "aux2", "aux0", "auxz0"):
                if isinstance(state.raw.get(key), dict):
                    aux_key = key
                    break
            aux_key = aux_key or "aux1"
            _LOGGER.warning(
                "TCX light aux not discovered yet; commanding %s as fallback",
                aux_key,
            )

        want_st = 1 if on else 0
        raw_aux = state.raw.get(aux_key) if isinstance(state.raw.get(aux_key), dict) else {}
        cmd_clr = color
        if cmd_clr is None:
            try:
                cmd_clr = int(raw_aux.get("currClr") or raw_aux.get("cmdClr") or 1)
            except (TypeError, ValueError):
                cmd_clr = 1

        _LOGGER.info(
            "TCX light %s via %s (color_index=%s session_token=%s)",
            "on" if on else "off",
            aux_key,
            cmd_clr,
            "yes" if self._session_client_token else "no",
        )

        if self.mock:
            await self._send_command(
                namespace=NAMESPACE_TCX,
                action=ACTION_SET_AUX_STATE,
                delta={aux_key: {"st": want_st}},
            )
            return

        # Try documented + observed envelopes. Do NOT optimistic-merge — prior
        # runs showed setAuxState/tcx can be ignored while local state lied.
        # Wire convention (iaqualink-py / protocol ref): WS payload is the
        # inner desired delta plus clientToken, e.g. {"aux0":{"st":1},...}.
        simple = {aux_key: {"st": want_st}}
        with_color = {aux_key: {"st": want_st, "cmdClr": int(cmd_clr)}}
        # liptonj /statecontrol body shape: {"desired": {...}} (no state wrap).
        desired_only = {"desired": {aux_key: {"st": want_st}}}
        state_desired = {"state": {"desired": {aux_key: {"st": want_st}}}}
        variants: list[tuple[str, str, dict[str, Any]]] = []
        if aux_key.startswith("auxz"):
            variants.extend(
                [
                    (NAMESPACE_ZIGBEE, ACTION_SET_ZIGBEE_STATE, simple),
                    ("zig", ACTION_SET_ZIGBEE_STATE, simple),
                    ("zig", ACTION_SET_AUX_STATE, simple),
                    ("zig", ACTION_SET_STATE, desired_only),
                ]
            )
        else:
            variants.extend(
                [
                    # Live-confirmed on RJEB… hardware: setAuxState with the
                    # MQTT-style state.desired wrapper + real clientToken.
                    (NAMESPACE_TCX, ACTION_SET_AUX_STATE, state_desired),
                    (NAMESPACE_TCX, ACTION_SET_STATE, state_desired),
                    (NAMESPACE_TCX, ACTION_SET_AUX_STATE, simple),
                    (NAMESPACE_TCX, ACTION_SET_AUX_STATE, with_color),
                    (NAMESPACE_TCX, ACTION_SET_STATE, simple),
                    (NAMESPACE_TCX, ACTION_SET_STATE, desired_only),
                    (NAMESPACE_PIB, ACTION_SET_AUX_STATE, state_desired),
                    (NAMESPACE_PIB, ACTION_SET_AUX_STATE, simple),
                    (NAMESPACE_PIB, ACTION_SET_AUX_LIGHT, with_color),
                    # liptonj docs: light commands sometimes use namespace "zig".
                    ("zig", ACTION_SET_AUX_STATE, simple),
                    ("zig", ACTION_SET_STATE, desired_only),
                    (NAMESPACE_ZIGBEE, ACTION_SET_ZIGBEE_STATE, simple),
                ]
            )

        for namespace, action, delta in variants:
            frames_before = len(self._ws_debug_frames)
            try:
                await self._send_command(
                    namespace=namespace,
                    action=action,
                    delta=delta,
                    optimistic=False,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "TCX light variant %s/%s failed to send: %s",
                    namespace,
                    action,
                    err,
                )
                continue

            if await self._async_wait_aux_remote(
                aux_key, want_st, frames_before=frames_before, timeout=5.0
            ):
                _LOGGER.info(
                    "TCX light confirmed via %s/%s (remote st=%s)",
                    namespace,
                    action,
                    want_st,
                )
                if on and color is not None and action != ACTION_SET_AUX_LIGHT:
                    try:
                        await self._send_command(
                            namespace=NAMESPACE_PIB,
                            action=ACTION_SET_AUX_LIGHT,
                            delta={aux_key: {"cmdClr": int(color)}},
                            optimistic=False,
                        )
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug("color follow-up failed", exc_info=True)
                # Keep local cache in sync with the confirmed remote state.
                self._reported = _deep_merge(
                    self._reported, {aux_key: {"st": want_st}}
                )
                self._notify()
                return

            _LOGGER.warning(
                "TCX light variant %s/%s produced no remote echo "
                "(frames=%d→%d, st=%s)",
                namespace,
                action,
                frames_before,
                len(self._ws_debug_frames),
                (self._reported.get(aux_key) or {}).get("st"),
            )

        raise TcxApiError(
            f"Light command sent but controller never echoed st={want_st} "
            f"for {aux_key}. Tried {len(variants)} websocket envelopes."
        )

    async def async_set_water_feature(self, on: bool) -> None:
        state = self.get_state()
        aux_key = state.water_feature_key
        if not aux_key:
            # Fall back to first WF-named aux, else aux0.
            for key, value in state.raw.items():
                if key.startswith("aux") and isinstance(value, dict):
                    app = str(value.get("app") or "")
                    fr = str(value.get("fr") or "").lower()
                    if app == "WF" or "waterfall" in fr or "water feature" in fr:
                        aux_key = key
                        break
            aux_key = aux_key or "aux0"
        await self._send_command(
            namespace=NAMESPACE_TCX,
            action=ACTION_SET_AUX_STATE,
            delta={aux_key: {"st": 1 if on else 0}},
        )

    async def async_close(self) -> None:
        await self._ws_close_quiet()
