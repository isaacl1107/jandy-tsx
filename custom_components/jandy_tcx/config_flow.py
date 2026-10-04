"""Config flow for Jandy AquaLink TCX."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TcxApiError, TcxAuthError, TcxClient
from .const import (
    CONF_EMAIL,
    CONF_MOCK,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_SCHEDULES,
    CONF_SERIAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .schedule import DEFAULT_SCHEDULES

_LOGGER = logging.getLogger(__name__)

STEP_USER = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_MOCK, default=False): bool,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Jandy TCX."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._mock = False
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
            self._mock = bool(user_input.get(CONF_MOCK, False))
            session = async_get_clientsession(self.hass)
            client = TcxClient(
                session,
                self._email,
                self._password,
                mock=self._mock,
            )
            try:
                await client.async_login()
                self._devices = await client.async_list_tcx_devices()
            except TcxAuthError:
                errors["base"] = "invalid_auth"
            except (TcxApiError, aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.warning("TCX connection failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not self._devices:
                    errors["base"] = "no_devices"
                elif len(self._devices) == 1:
                    return await self._async_create_from_device(self._devices[0])
                else:
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER,
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            serial = user_input[CONF_SERIAL]
            device = next(
                item
                for item in self._devices
                if item["serial_number"] == serial
            )
            return await self._async_create_from_device(device)

        options = {
            item["serial_number"]: item.get("name") or item["serial_number"]
            for item in self._devices
        }
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({vol.Required(CONF_SERIAL): vol.In(options)}),
        )

    async def _async_create_from_device(
        self, device: dict[str, Any]
    ) -> FlowResult:
        serial = str(device["serial_number"])
        await self.async_set_unique_id(serial)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=device.get("name") or f"TCX {serial}",
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_SERIAL: serial,
                CONF_MOCK: self._mock,
                CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
                CONF_SCHEDULES: DEFAULT_SCHEDULES,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Options: poll interval + JSON schedules."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        import json

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                schedules = json.loads(user_input["schedules_json"])
                if not isinstance(schedules, list):
                    raise ValueError("schedules must be a list")
            except (ValueError, json.JSONDecodeError):
                errors["base"] = "invalid_schedules"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                        CONF_SCHEDULES: schedules,
                    },
                )

        current = self._entry.options.get(
            CONF_SCHEDULES,
            self._entry.data.get(CONF_SCHEDULES, DEFAULT_SCHEDULES),
        )
        poll = self._entry.options.get(
            CONF_POLL_INTERVAL,
            self._entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLL_INTERVAL, default=int(poll)): vol.All(
                        vol.Coerce(int), vol.Range(min=15, max=300)
                    ),
                    vol.Required(
                        "schedules_json",
                        default=json.dumps(current, indent=2),
                    ): str,
                }
            ),
            errors=errors,
        )
