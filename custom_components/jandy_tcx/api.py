"""Zodiac / iAquaLink cloud client for AquaLink TCX controllers."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
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
    ACTION_SUBSCRIBE,
    API_KEY,
    DEVICES_URL,
    LOGIN_URL,
    NAMESPACE_AUTHORIZATION,
    NAMESPACE_FILTRATION,
    NAMESPACE_TCX,
    REFRESH_URL,
    SERVICE_AUTHORIZATION,
    SERVICE_STATE_CONTROLLER,
    SHADOW_URL,
    TEMP_SCALE,
    USER_AGENT,
    WS_URL,
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

    # Prefer a pool-light aux relay when present.
    for key, value in reported.items():
        if not key.startswith("aux") or not isinstance(value, dict):
            continue
        app = str(value.get("app") or "")
        et = str(value.get("et") or "")
        if app in {"POOL_LT", "POOL_LIGHT"} or et in {"JL", "IB", "HU", "WL"}:
            state.light_on = int(value.get("st") or 0) == 1
            state.light_color = int(value.get("currClr") or value.get("cmdClr") or 0)
            state.light_name = str(value.get("fr") or "Pool Light")
            break

    for key in ("auxz0",):
        zig = reported.get(key)
        if isinstance(zig, dict) and "st" in zig and not state.light_on:
            state.light_on = int(zig.get("st") or 0) == 1
            state.light_name = str(zig.get("fr") or "Pool Light")

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

        payload = {
            "api_key": API_KEY,
            "email": self._email,
            "password": self._password,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        async with self._session.post(
            LOGIN_URL, json=payload, headers=headers, timeout=30
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status in (401, 403):
                raise TcxAuthError(f"Login rejected ({resp.status})")
            if resp.status >= 400:
                raise TcxApiError(f"Login failed ({resp.status}): {body}")

        oauth = body.get("userPoolOAuth") or {}
        token = oauth.get("IdToken")
        if not token:
            raise TcxAuthError("Login response missing IdToken")
        self._id_token = token
        self._auth_token = body.get("authentication_token")
        self._user_id = str(body.get("id"))
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
        return body

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
            REFRESH_URL, json=payload, headers=headers, timeout=30
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

    async def async_list_tcx_devices(self) -> list[dict[str, Any]]:
        if self.mock:
            return [
                {
                    "serial_number": self.serial or "MOCKTCX01",
                    "name": "Backyard Pool",
                    "device_type": "tcx",
                }
            ]

        await self._ensure_token()
        params = {
            "api_key": API_KEY,
            "authentication_token": self._auth_token,
            "user_id": self._user_id,
        }
        headers = {
            "Accept": "application/json",
            "Authorization": self._id_token or "",
            "User-Agent": USER_AGENT,
        }
        async with self._session.get(
            DEVICES_URL, params=params, headers=headers, timeout=30
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status in (401, 403):
                raise TcxAuthError("Device list unauthorized")
            if resp.status >= 400:
                raise TcxApiError(f"Device list failed ({resp.status})")

        devices = body if isinstance(body, list) else body.get("devices", [])
        return [
            device
            for device in devices
            if isinstance(device, dict)
            and str(device.get("device_type", "")).lower() == "tcx"
            and device.get("serial_number")
        ]

    async def async_get_shadow(self) -> dict[str, Any]:
        if self.mock:
            self._reported = dict(self._mock_state)
            return {"state": {"reported": self._reported}}

        if not self.serial:
            raise TcxApiError("No TCX serial configured")
        token = await self._ensure_token()
        url = SHADOW_URL.format(serial=self.serial)
        headers = {
            "Authorization": token,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        async with self._session.get(url, headers=headers, timeout=30) as resp:
            if resp.status in (401, 403):
                self._id_token = None
                token = await self._ensure_token()
                headers["Authorization"] = token
                async with self._session.get(
                    url, headers=headers, timeout=30
                ) as retry:
                    if retry.status >= 400:
                        raise TcxApiError(f"Shadow GET failed ({retry.status})")
                    body = await retry.json(content_type=None)
            elif resp.status >= 400:
                text = await resp.text()
                raise TcxApiError(f"Shadow GET failed ({resp.status}): {text}")
            else:
                body = await resp.json(content_type=None)

        reported = (body.get("state") or {}).get("reported") or {}
        if isinstance(reported, dict):
            self._reported = reported
            self._notify()
        return body

    def _client_token(self) -> str:
        if self._user_id and self._auth_token and self._app_client_id:
            return f"{self._user_id}|{self._auth_token}|{self._app_client_id}"
        if self._user_id:
            return f"{self._user_id}|{uuid.uuid4().hex}"
        return uuid.uuid4().hex

    async def async_connect_ws(self) -> None:
        if self.mock:
            return
        async with self._ws_lock:
            if self._ws and not self._ws.closed:
                return
            await self._ensure_token()
            ssl_context = ssl.create_default_context()
            headers = {
                "Authorization": self._id_token or "",
                "User-Agent": USER_AGENT,
            }
            self._ws = await self._session.ws_connect(
                WS_URL,
                headers=headers,
                ssl=ssl_context,
                heartbeat=30,
                autoping=True,
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
            if self._ws_task is None or self._ws_task.done():
                self._ws_task = asyncio.create_task(self._ws_receive_loop())

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
        # Full Authorization payload replaces the cache; deltas merge.
        if frame.get("service") == SERVICE_AUTHORIZATION:
            self._reported = merged
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
        # Prefer classic aux light when present in state.
        aux_key = "aux1"
        for key, value in self.get_state().raw.items():
            if key.startswith("aux") and isinstance(value, dict):
                app = str(value.get("app") or "")
                if app in {"POOL_LT", "POOL_LIGHT"}:
                    aux_key = key
                    break
        delta: dict[str, Any] = {aux_key: {"st": 1 if on else 0}}
        await self._send_command(
            namespace=NAMESPACE_TCX,
            action=ACTION_SET_AUX_STATE,
            delta=delta,
        )
        if on and color is not None:
            await self._send_command(
                namespace="pib",
                action=ACTION_SET_AUX_LIGHT,
                delta={aux_key: {"cmdClr": int(color)}},
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
