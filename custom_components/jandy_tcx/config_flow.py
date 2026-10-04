"""Config flow for Jandy AquaLink TCX."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
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
    TEMP_MAX_F,
    TEMP_MIN_F,
)
from .light import JANDY_EFFECTS
from .schedule import DEFAULT_SCHEDULES

_LOGGER = logging.getLogger(__name__)

DAY_OPTIONS = [
    selector.SelectOptionDict(value="0", label="Monday"),
    selector.SelectOptionDict(value="1", label="Tuesday"),
    selector.SelectOptionDict(value="2", label="Wednesday"),
    selector.SelectOptionDict(value="3", label="Thursday"),
    selector.SelectOptionDict(value="4", label="Friday"),
    selector.SelectOptionDict(value="5", label="Saturday"),
    selector.SelectOptionDict(value="6", label="Sunday"),
]

TARGET_OPTIONS = [
    selector.SelectOptionDict(value="heater", label="Heater"),
    selector.SelectOptionDict(value="pump", label="Filter pump"),
    selector.SelectOptionDict(value="light", label="Pool light"),
    selector.SelectOptionDict(value="water_feature", label="Water feature"),
]

LIGHT_COLOR_OPTIONS = [
    selector.SelectOptionDict(value=str(idx), label=name)
    for idx, name in enumerate(JANDY_EFFECTS, start=1)
]

STEP_USER = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_MOCK, default=False): bool,
    }
)


def _normalize_hhmm(value: Any, default: str = "10:00") -> str:
    """Normalize HA time selector values to HH:MM."""
    if value is None:
        return default
    if hasattr(value, "hour") and hasattr(value, "minute"):
        return f"{int(value.hour):02d}:{int(value.minute):02d}"
    text = str(value)
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return default


def _schedule_label(slot: dict[str, Any]) -> str:
    days = slot.get("days") or []
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    day_txt = ",".join(day_names[int(d)] for d in days if 0 <= int(d) <= 6) or "No days"
    enabled = "on" if slot.get("enabled", True) else "off"
    target = str(slot.get("target", "heater"))
    start = slot.get("start", "??:??")
    end = slot.get("end", "??:??")
    suffix = ""
    if target == "heater" and slot.get("setpoint_f") is not None:
        suffix = f" @ {float(slot['setpoint_f']):g}°F"
    elif target == "pump" and slot.get("rpm") is not None:
        suffix = f" @ {int(slot['rpm'])} RPM"
    elif target == "light" and slot.get("light_color") is not None:
        idx = int(slot["light_color"])
        if 1 <= idx <= len(JANDY_EFFECTS):
            suffix = f" @ {JANDY_EFFECTS[idx - 1]}"
        else:
            suffix = f" @ color {idx}"
    return f"{target}: {start}-{end} ({day_txt}) [{enabled}]{suffix}"


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
            except TcxAuthError as err:
                _LOGGER.warning("TCX login failed: %s", err)
                errors["base"] = "invalid_auth"
            except (TcxApiError, aiohttp.ClientError, TimeoutError, ValueError) as err:
                _LOGGER.warning("TCX login connection failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                try:
                    all_devices = await client.async_list_devices()
                except TcxAuthError as err:
                    _LOGGER.warning("TCX device list unauthorized: %s", err)
                    errors["base"] = "invalid_auth"
                except (TcxApiError, aiohttp.ClientError, TimeoutError) as err:
                    _LOGGER.warning("TCX device list failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    self._devices = [
                        device
                        for device in all_devices
                        if isinstance(device, dict)
                        and str(device.get("device_type", "")).lower() == "tcx"
                        and device.get("serial_number")
                    ]
                    if not self._devices:
                        types = sorted(
                            {
                                str(d.get("device_type"))
                                for d in all_devices
                                if isinstance(d, dict) and d.get("device_type")
                            }
                        )
                        _LOGGER.warning(
                            "No TCX devices on account (found types=%s)", types
                        )
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
    """Options UI for poll interval and weekly schedules."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry
        self._schedules: list[dict[str, Any]] = []
        self._poll_interval = DEFAULT_POLL_INTERVAL
        self._edit_id: str | None = None
        self._form_target: str | None = None

    def _load_working_copy(self) -> None:
        if self._schedules:
            return
        raw = self._entry.options.get(
            CONF_SCHEDULES,
            self._entry.data.get(CONF_SCHEDULES, DEFAULT_SCHEDULES),
        )
        self._schedules = [dict(item) for item in raw]
        self._poll_interval = int(
            self._entry.options.get(
                CONF_POLL_INTERVAL,
                self._entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            )
        )

    def _save(self) -> FlowResult:
        return self.async_create_entry(
            title="",
            data={
                CONF_POLL_INTERVAL: int(self._poll_interval),
                CONF_SCHEDULES: self._schedules,
            },
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the top-level options menu."""
        self._load_working_copy()
        return self.async_show_menu(
            step_id="init",
            menu_options=["schedules", "poll", "save"],
            description_placeholders={
                "count": str(len(self._schedules)),
                "poll": str(self._poll_interval),
            },
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Persist working schedule/poll changes."""
        self._load_working_copy()
        return self._save()

    async def async_step_poll(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._load_working_copy()
        if user_input is not None:
            self._poll_interval = int(user_input[CONF_POLL_INTERVAL])
            return await self.async_step_init()
        return self.async_show_form(
            step_id="poll",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL, default=self._poll_interval
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=15,
                            max=300,
                            step=5,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="seconds",
                        )
                    ),
                }
            ),
        )

    async def async_step_schedules(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._load_working_copy()
        if user_input is not None:
            action = user_input["schedule_action"]
            if action == "add":
                self._edit_id = None
                self._form_target = None
                return await self.async_step_schedule_target()
            if action.startswith("edit:"):
                self._edit_id = action.split(":", 1)[1]
                defaults = self._slot_defaults()
                self._form_target = str(defaults.get("target") or "heater")
                return await self.async_step_schedule_form()
            if action.startswith("delete:"):
                self._edit_id = action.split(":", 1)[1]
                return await self.async_step_schedule_delete()
            if action == "back":
                return await self.async_step_init()

        options: dict[str, str] = {"add": "Add schedule"}
        for slot in self._schedules:
            sid = str(slot["id"])
            options[f"edit:{sid}"] = f"Edit — {_schedule_label(slot)}"
            options[f"delete:{sid}"] = f"Delete — {_schedule_label(slot)}"
        options["back"] = "Back"

        return self.async_show_form(
            step_id="schedules",
            data_schema=vol.Schema(
                {
                    vol.Required("schedule_action"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=key, label=label)
                                for key, label in options.items()
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={"count": str(len(self._schedules))},
        )

    def _slot_defaults(self) -> dict[str, Any]:
        if self._edit_id:
            for slot in self._schedules:
                if str(slot.get("id")) == self._edit_id:
                    return slot
        return {
            "id": uuid.uuid4().hex[:10],
            "target": self._form_target or "heater",
            "days": [0, 1, 2, 3, 4],
            "start": "10:00",
            "end": "18:00",
            "enabled": True,
            "setpoint_f": 84,
            "rpm": 2500,
            "light_color": 1,
        }

    def _pump_rpm_bounds(self) -> tuple[int, int, int]:
        min_rpm, max_rpm, default = 1000, 3450, 2500
        try:
            coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
            state = getattr(coordinator, "data", None) if coordinator else None
            if state is not None:
                min_rpm = int(getattr(state, "pump_min_rpm", None) or min_rpm)
                max_rpm = int(getattr(state, "pump_max_rpm", None) or max_rpm)
                if getattr(state, "pump_rpm", None):
                    default = int(state.pump_rpm)
        except (TypeError, ValueError, AttributeError, KeyError):
            pass
        default = max(min_rpm, min(max_rpm, default))
        return min_rpm, max_rpm, default

    async def async_step_schedule_target(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick equipment first so the next form only shows relevant fields."""
        self._load_working_copy()
        defaults = self._slot_defaults()
        if user_input is not None:
            self._form_target = str(user_input["target"])
            return await self.async_step_schedule_form()

        return self.async_show_form(
            step_id="schedule_target",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "target",
                        default=str(defaults.get("target") or "heater"),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=TARGET_OPTIONS,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={
                "mode": "Edit schedule" if self._edit_id else "Add schedule"
            },
        )

    async def async_step_schedule_form(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._load_working_copy()
        defaults = self._slot_defaults()
        target = str(self._form_target or defaults.get("target") or "heater")
        self._form_target = target
        errors: dict[str, str] = {}
        min_rpm, max_rpm, default_rpm = self._pump_rpm_bounds()

        if user_input is not None:
            start = _normalize_hhmm(user_input["start"], defaults["start"])
            end = _normalize_hhmm(user_input["end"], defaults["end"])
            days = [int(day) for day in user_input.get("days", [])]
            if not days:
                errors["days"] = "no_days"
            else:
                setpoint = user_input.get("setpoint_f")
                rpm = user_input.get("rpm")
                light_color = user_input.get("light_color")
                slot = {
                    "id": defaults["id"],
                    "target": target,
                    "days": days,
                    "start": start,
                    "end": end,
                    "enabled": bool(user_input.get("enabled", True)),
                    "setpoint_f": (
                        float(setpoint)
                        if target == "heater" and setpoint is not None
                        else None
                    ),
                    "rpm": (
                        int(rpm) if target == "pump" and rpm is not None else None
                    ),
                    "light_color": (
                        int(light_color)
                        if target == "light" and light_color is not None
                        else None
                    ),
                }
                replaced = False
                for idx, existing in enumerate(self._schedules):
                    if str(existing.get("id")) == str(slot["id"]):
                        self._schedules[idx] = slot
                        replaced = True
                        break
                if not replaced:
                    self._schedules.append(slot)
                self._edit_id = None
                self._form_target = None
                return await self.async_step_schedules()

        schema_dict: dict[Any, Any] = {
            vol.Required(
                "days",
                default=[str(d) for d in defaults.get("days", [])],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=DAY_OPTIONS,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                "start", default=_normalize_hhmm(defaults.get("start"), "10:00")
            ): selector.TimeSelector(),
            vol.Required(
                "end", default=_normalize_hhmm(defaults.get("end"), "18:00")
            ): selector.TimeSelector(),
        }

        if target == "heater":
            schema_dict[
                vol.Required(
                    "setpoint_f",
                    default=(
                        float(defaults["setpoint_f"])
                        if defaults.get("setpoint_f") is not None
                        else 84.0
                    ),
                )
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=TEMP_MIN_F,
                    max=TEMP_MAX_F,
                    step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="°F",
                )
            )
        elif target == "pump":
            rpm_default = (
                int(defaults["rpm"])
                if defaults.get("rpm") is not None
                else default_rpm
            )
            rpm_default = max(min_rpm, min(max_rpm, rpm_default))
            schema_dict[
                vol.Required("rpm", default=rpm_default)
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=min_rpm,
                    max=max_rpm,
                    step=50,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="RPM",
                )
            )
        elif target == "light":
            color_default = str(
                int(defaults["light_color"])
                if defaults.get("light_color") is not None
                else 1
            )
            schema_dict[
                vol.Required("light_color", default=color_default)
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=LIGHT_COLOR_OPTIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        schema_dict[
            vol.Required("enabled", default=bool(defaults.get("enabled", True)))
        ] = selector.BooleanSelector()

        target_labels = {opt["value"]: opt["label"] for opt in TARGET_OPTIONS}
        return self.async_show_form(
            step_id="schedule_form",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "mode": "Edit schedule" if self._edit_id else "Add schedule",
                "equipment": target_labels.get(target, target),
            },
        )

    async def async_step_schedule_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._load_working_copy()
        slot = next(
            (item for item in self._schedules if str(item.get("id")) == self._edit_id),
            None,
        )
        if slot is None:
            self._edit_id = None
            return await self.async_step_schedules()

        if user_input is not None:
            if user_input.get("confirm"):
                self._schedules = [
                    item
                    for item in self._schedules
                    if str(item.get("id")) != self._edit_id
                ]
            self._edit_id = None
            return await self.async_step_schedules()

        return self.async_show_form(
            step_id="schedule_delete",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): selector.BooleanSelector()}
            ),
            description_placeholders={"label": _schedule_label(slot)},
        )
