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
    ACTION_GET_STATE,
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
    SUB_SHADOW_URL,
    SUB_SHADOW_URL_V2,
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


def _tenths_to_f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value) / TEMP_SCALE, 1)
    except (TypeError, ValueError):
        return None


def _f_to_tenths(temp_f: float) -> int:
    return int(round(float(temp_f) * TEMP_SCALE))


def _merge_reported(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge Authorization namespace-keyed payload or flat reported state."""
    direct = (payload.get("state") or {}).get("reported")
    if isinstance(direct, dict) and direct:
        return dict(direct)

    merged: dict[str, Any] = {}
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        reported = (value.get("state") or {}).get("reported")
        if isinstance(reported, dict):
            merged.update(reported)
        elif "metadata" not in value and any(
            key in value for key in ("water", "filt0", "TspBdy0", "pool", "lvh1")
        ):
            merged.update(value)
    return merged


def parse_reported(reported: dict[str, Any], *, serial: str = "") -> TcxState:
    """Parse a flat TCX reported tree into TcxState."""
    state = TcxState(serial=serial or str(reported.get("sn") or ""), raw=reported)
    state.name = str(reported.get("name") or reported.get("fr") or "Pool")
    aws = reported.get("aws") or {}
    state.online = str(aws.get("status", "")).lower() == "connected" or bool(
        reported
    )

    water = reported.get("water") or {}
    if isinstance(water, dict) and "value" in water:
        state.water_temp_f = _tenths_to_f(water.get("value"))
    elif "waterTemp" in reported:
        state.water_temp_f = _tenths_to_f(reported.get("waterTemp"))

    air = reported.get("air") or {}
    if isinstance(air, dict) and "value" in air:
        state.air_temp_f = _tenths_to_f(air.get("value"))
    elif "airTemp" in reported:
        # Some payloads publish airTemp already in whole °F.
        try:
            air_raw = float(reported["airTemp"])
            state.air_temp_f = (
                round(air_raw / TEMP_SCALE, 1) if air_raw > 200 else air_raw
            )
        except (TypeError, ValueError):
            state.air_temp_f = None

    tsp = reported.get("TspBdy0") or {}
    if isinstance(tsp, dict):
        state.heater_setpoint_f = _tenths_to_f(tsp.get("waterTempSet"))
        state.heater_enabled = bool(tsp.get("heatEnabled"))
        if tsp.get("name"):
            state.heater_name = str(tsp["name"]) + " Heater"

    lvh = reported.get("lvh1") or {}
    if isinstance(lvh, dict):
        state.heater_running = int(lvh.get("en") or 0) == 1
        if lvh.get("fr"):
            state.heater_name = str(lvh["fr"])

    filt = reported.get("filt0") or {}
    pool = reported.get("pool") or {}
    if isinstance(filt, dict) and "st" in filt:
        state.pump_on = int(filt.get("st") or 0) == 1
    elif isinstance(pool, dict) and "st" in pool:
        state.pump_on = int(pool.get("st") or 0) == 1

    ecm = reported.get("ecm0") or {}
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
    if isinstance(swc, dict) and swc.get("swc") is not None:
        try:
            state.swc_percent = int(swc["swc"])
        except (TypeError, ValueError):
            state.swc_percent = None

    return state


def mock_reported(serial: str = "MOCKTCX01") -> dict[str, Any]:
    """Synthetic reported tree for demo / offline setup."""
    return {
        "sn": serial,
        "model": "TCX-1",
        "deviceType": "tcx",
        "name": "Backyard Pool",
        "aws": {"status": "connected", "timestamp": int(time.time())},
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
        self._reported: dict[str, Any] = {}
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
            self._app_client_id = (
                oauth.get("appClientId")
                or body.get("appClientId")
                or oauth.get("ClientId")
            )
            try:
                expires_in = int(oauth.get("ExpiresIn", 3600))
            except (TypeError, ValueError):
                expires_in = 3600
            self._token_expiry = time.monotonic() + max(60, expires_in - 300)
            _LOGGER.info(
                "Zodiac login OK with %s/…%s for %s",
                key_field,
                api_key[-4:],
                email,
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
            self._reported = {**self._reported, **reported}
            self._notify()
        # Pull PIB/Zigbee sub-shadows when equipment flags say they exist.
        # Main REST shadow often omits aux/lights entirely.
        await self._async_fetch_equipment_subshadows(reported)
        return body

    async def _async_fetch_equipment_subshadows(
        self, reported: dict[str, Any] | None = None
    ) -> None:
        """Best-effort REST fetch of sub-shadows listed under equipment."""
        if self.mock:
            return
        source = reported if isinstance(reported, dict) else self._reported
        equipment = source.get("equipment")
        if not isinstance(equipment, dict) or not equipment:
            # Still try the common light-bearing suffixes.
            suffixes = ("_pib0", "_zig", "_filt", "_ecm")
        else:
            mapping = {
                "pib0": "_pib0",
                "zig": "_zig",
                "filt": "_filt",
                "ecm": "_ecm",
                "fea": "_fea",
                "swc": "_swc",
            }
            suffixes = tuple(
                mapping[key] for key in mapping if key in equipment
            ) or ("_pib0", "_zig")

        token = await self._ensure_token()
        timeout = aiohttp.ClientTimeout(total=20)
        for suffix in suffixes:
            for url_template, use_sig in (
                (SUB_SHADOW_URL, False),
                (SUB_SHADOW_URL_V2, True),
            ):
                url = url_template.format(serial=self.serial, suffix=suffix)
                headers = {
                    "Authorization": token,
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                }
                params: dict[str, str] = {}
                if use_sig and self._user_id:
                    params["signature"] = self._sign(
                        [f"{self.serial}{suffix}".upper(), str(self._user_id)]
                    )
                    # Also try serial-only signature variants below on failure.
                try:
                    async with self._session.get(
                        url, headers=headers, params=params or None, timeout=timeout
                    ) as resp:
                        text = await resp.text()
                        if resp.status >= 400:
                            _LOGGER.debug(
                                "Sub-shadow %s via %s failed (%s): %s",
                                suffix,
                                "v2" if use_sig else "v1",
                                resp.status,
                                text[:120],
                            )
                            continue
                        try:
                            body = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                except (aiohttp.ClientError, TimeoutError) as err:
                    _LOGGER.debug("Sub-shadow %s transport error: %s", suffix, err)
                    continue

                chunk = (body.get("state") or {}).get("reported")
                if not isinstance(chunk, dict) or not chunk:
                    # Some sub-shadows return the object at the root.
                    if isinstance(body, dict) and any(
                        key.startswith(("aux", "filt", "ecm", "water"))
                        for key in body
                    ):
                        chunk = body
                    else:
                        continue
                _LOGGER.info(
                    "Merged sub-shadow %s (%d keys)", suffix, len(chunk)
                )
                self._reported.update(chunk)
                self._notify()
                break  # success for this suffix

    async def async_set_desired(
        self, delta: dict[str, Any], *, suffix: str = ""
    ) -> None:
        """POST a desired-state delta via REST shadow (write fallback)."""
        if self.mock:
            self._apply_mock_delta(delta)
            self._notify()
            return
        token = await self._ensure_token()
        if suffix:
            url = SUB_SHADOW_URL_V2.format(serial=self.serial, suffix=suffix)
        else:
            url = SHADOW_URL.format(serial=self.serial)
        headers = {
            "Authorization": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        payload = {"state": {"desired": delta}}
        timeout = aiohttp.ClientTimeout(total=30)
        async with self._session.post(
            url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise TcxApiError(
                    f"Shadow desired POST {suffix or 'main'} failed "
                    f"({resp.status}): {text[:200]}"
                )
        # Optimistic merge of desired into local reported for UI.
        for key, value in delta.items():
            existing = self._reported.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                existing.update(value)
            else:
                self._reported[key] = value
        self._notify()

    def _client_token(self) -> str:
        if self._user_id and self._auth_token and self._app_client_id:
            return f"{self._user_id}|{self._auth_token}|{self._app_client_id}"
        if self._user_id:
            return f"{self._user_id}|{uuid.uuid4().hex}"
        return uuid.uuid4().hex

    async def async_connect_ws(self, *, wait_for_auth: bool = True) -> None:
        if self.mock:
            return
        freshly_connected = False
        async with self._ws_lock:
            already_open = bool(self._ws and not self._ws.closed)
            if not already_open:
                await self._ensure_token()
                self._ws_auth_event.clear()
                self._ws_debug_frames.clear()
                # Use the HA aiohttp session SSL context — never call
                # ssl.create_default_context() on the event loop (blocking I/O).
                headers = {
                    "Authorization": self._id_token or "",
                    "User-Agent": USER_AGENT_MOBILE,
                }
                self._ws = await self._session.ws_connect(
                    WS_URL,
                    headers=headers,
                    heartbeat=30,
                    autoping=True,
                )
                # Start the receiver BEFORE subscribe so the Authorization
                # full-state push cannot be missed.
                if self._ws_task is None or self._ws_task.done():
                    self._ws_task = asyncio.create_task(self._ws_receive_loop())
                subscribe = {
                    "action": ACTION_SUBSCRIBE,
                    "version": 1,
                    "namespace": NAMESPACE_AUTHORIZATION,
                    "service": SERVICE_AUTHORIZATION,
                    "payload": {"userId": int(self._user_id or 0)},
                    "target": self.serial,
                }
                await self._ws.send_json(subscribe)
                # Explicit full-state request used by the official client.
                await self._ws.send_json(
                    {
                        "action": ACTION_GET_STATE,
                        "version": 1,
                        "namespace": NAMESPACE_AUTHORIZATION,
                        "service": SERVICE_AUTHORIZATION,
                        "target": self.serial,
                        "payload": {"clientToken": self._client_token()},
                    }
                )
                freshly_connected = True

        # Authorization full-state carries aux/lights from pib0 + zig namespaces.
        if wait_for_auth and freshly_connected and not self._ws_auth_event.is_set():
            try:
                await asyncio.wait_for(self._ws_auth_event.wait(), timeout=12.0)
            except TimeoutError:
                _LOGGER.warning(
                    "TCX websocket Authorization state not received within 12s "
                    "(%d frames seen); trying REST sub-shadows for aux/lights",
                    len(self._ws_debug_frames),
                )
                for frame in self._ws_debug_frames[:8]:
                    _LOGGER.warning(
                        "WS frame sample: service=%s namespace=%s keys=%s",
                        frame.get("service"),
                        frame.get("namespace"),
                        sorted((frame.get("payload") or {}).keys())
                        if isinstance(frame.get("payload"), dict)
                        else type(frame.get("payload")).__name__,
                    )
                try:
                    await self._async_fetch_equipment_subshadows()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Sub-shadow fallback failed", exc_info=True)

    async def _ws_receive_loop(self) -> None:
        assert self._ws is not None
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
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("TCX websocket receive loop failed")
        finally:
            _LOGGER.warning("TCX websocket closed")

    async def _handle_ws_frame(self, frame: dict[str, Any]) -> None:
        if len(self._ws_debug_frames) < 20:
            # Keep a redacted structural sample for diagnostics.
            sample = {
                "service": frame.get("service"),
                "namespace": frame.get("namespace"),
                "action": frame.get("action"),
                "payload": frame.get("payload")
                if not isinstance(frame.get("payload"), dict)
                else {
                    key: (
                        sorted(value.keys())
                        if isinstance(value, dict)
                        else type(value).__name__
                    )
                    for key, value in frame["payload"].items()
                },
            }
            self._ws_debug_frames.append(sample)
            _LOGGER.debug("TCX WS frame: %s", sample)

        payload = frame.get("payload")
        if not isinstance(payload, dict):
            return
        merged = _merge_reported(payload)
        if not merged:
            # Delta-style desired/reported nested under state
            state = payload.get("state") or {}
            for key in ("reported", "desired"):
                chunk = state.get(key)
                if isinstance(chunk, dict) and chunk:
                    self._reported.update(chunk)
                    self._notify()
                    return
            return

        service = str(frame.get("service") or "")
        looks_like_auth = service == SERVICE_AUTHORIZATION or bool(
            _AUTH_NAMESPACE_KEYS.intersection(payload)
        )
        # Authorization full-state replaces cache; streamer deltas merge.
        if looks_like_auth and (
            "aux" in str(merged.keys())
            or "water" in merged
            or "filt0" in merged
            or "pib0" in payload
            or service == SERVICE_AUTHORIZATION
        ):
            # Prefer merging so a sparse auth frame cannot wipe REST keys.
            self._reported = {**self._reported, **merged}
            # Promote to "have auth" when we gained aux/light-bearing keys
            # or the service explicitly says Authorization.
            if (
                service == SERVICE_AUTHORIZATION
                or any(key.startswith("aux") for key in merged)
                or "water" in merged
                or "filt0" in merged
            ):
                self._ws_auth_event.set()
        else:
            self._reported.update(merged)
        self._notify()

    async def _send_command(
        self, *, namespace: str, action: str, delta: dict[str, Any]
    ) -> None:
        if self.mock:
            self._apply_mock_delta(delta)
            self._notify()
            return

        await self.async_connect_ws()
        if self._ws is None or self._ws.closed:
            raise TcxApiError("WebSocket unavailable for command")
        frame = {
            "version": 1,
            "action": action,
            "namespace": namespace,
            "service": SERVICE_STATE_CONTROLLER,
            "target": self.serial,
            "payload": {**delta, "clientToken": self._client_token()},
        }
        await self._ws.send_json(frame)
        # Optimistic local merge so the UI feels responsive.
        self._reported.update(delta if all(isinstance(v, dict) for v in delta.values()) else {})
        for key, value in delta.items():
            existing = self._reported.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                existing.update(value)
            else:
                self._reported[key] = value
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

    async def async_set_filter_pump(self, on: bool) -> None:
        await self._send_command(
            namespace=NAMESPACE_FILTRATION,
            action=ACTION_SET_FILTER_PUMP_STATE,
            delta={"pool": {"st": 1 if on else 0}},
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
            delta={"TspBdy0": {"waterTempSet": _f_to_tenths(temp_f)}},
        )

    async def async_set_pump_rpm(self, rpm: int) -> None:
        await self._send_command(
            namespace=NAMESPACE_TCX,
            action=ACTION_SET_STATE,
            delta={"ecm0": {"cmdSpd": int(rpm)}},
        )

    async def async_set_light(self, on: bool, color: int | None = None) -> None:
        """Toggle pool light.

        On/off is always ``setAuxState`` (wired aux) or ``setZigbeeState``
        (auxz*). Color programs use ``setAuxLight`` with a 1-based index.
        """
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
            # Last resort so the entity stays controllable while discovery catches up.
            for key in ("aux1", "aux2", "aux0", "auxz0"):
                if isinstance(state.raw.get(key), dict):
                    aux_key = key
                    break
            aux_key = aux_key or "aux1"
            _LOGGER.warning(
                "TCX light aux not discovered yet; commanding %s as fallback",
                aux_key,
            )

        _LOGGER.info(
            "TCX light %s via %s (color_index=%s)",
            "on" if on else "off",
            aux_key,
            color,
        )

        delta = {aux_key: {"st": 1 if on else 0}}

        # On/off: confirmed wire path is setAuxState / setZigbeeState (not setAuxLight).
        ws_error: Exception | None = None
        try:
            if aux_key.startswith("auxz"):
                await self._send_command(
                    namespace=NAMESPACE_ZIGBEE,
                    action=ACTION_SET_ZIGBEE_STATE,
                    delta=delta,
                )
            else:
                await self._send_command(
                    namespace=NAMESPACE_TCX,
                    action=ACTION_SET_AUX_STATE,
                    delta=delta,
                )
        except Exception as err:  # noqa: BLE001
            ws_error = err
            _LOGGER.warning("TCX light WS command failed: %s", err)

        # REST desired-state fallback — lights live on the PIB sub-shadow.
        if not aux_key.startswith("auxz"):
            for suffix in ("_pib0", ""):
                try:
                    await self.async_set_desired(delta, suffix=suffix)
                    _LOGGER.info(
                        "TCX light REST desired posted to %s",
                        suffix or "main",
                    )
                    break
                except TcxApiError as err:
                    _LOGGER.debug(
                        "TCX light REST desired %s failed: %s",
                        suffix or "main",
                        err,
                    )
            else:
                if ws_error is not None:
                    raise TcxApiError(
                        f"Light command failed over WS and REST: {ws_error}"
                    ) from ws_error

        # Color is a separate PIB command; wire index is 1-based.
        if on and color is not None and not aux_key.startswith("auxz"):
            color_delta = {aux_key: {"cmdClr": int(color)}}
            try:
                await self._send_command(
                    namespace=NAMESPACE_PIB,
                    action=ACTION_SET_AUX_LIGHT,
                    delta=color_delta,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("setAuxLight WS failed; trying REST", exc_info=True)
                try:
                    await self.async_set_desired(color_delta, suffix="_pib0")
                except TcxApiError:
                    _LOGGER.debug("setAuxLight REST failed", exc_info=True)

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
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
