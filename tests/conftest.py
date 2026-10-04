"""Make custom_components importable without a full Home Assistant install."""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_homeassistant_stubs() -> None:
    if "homeassistant" in sys.modules:
        return

    def module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    module("homeassistant")
    const = module("homeassistant.const")

    class Platform(str):
        def __new__(cls, value: str):
            return str.__new__(cls, value)

    const.Platform = Platform
    const.ATTR_TEMPERATURE = "temperature"
    const.UnitOfTemperature = types.SimpleNamespace(FAHRENHEIT="°F")
    const.PERCENTAGE = "%"
    const.CONF_PASSWORD = "password"

    core = module("homeassistant.core")
    core.HomeAssistant = object
    core.ServiceCall = object
    core.callback = lambda fn: fn

    config_entries = module("homeassistant.config_entries")

    class _ConfigEntry:
        pass

    class _ConfigFlow:
        def __init_subclass__(cls, domain: str | None = None, **kwargs):
            super().__init_subclass__(**kwargs)

    class _OptionsFlow:
        pass

    config_entries.ConfigEntry = _ConfigEntry
    config_entries.ConfigFlow = _ConfigFlow
    config_entries.OptionsFlow = _OptionsFlow

    module("homeassistant.data_entry_flow").FlowResult = dict
    helpers = module("homeassistant.helpers")
    module("homeassistant.helpers.aiohttp_client").async_get_clientsession = lambda hass: None
    update = module("homeassistant.helpers.update_coordinator")

    class _Coordinator:
        def __init__(self, *args, **kwargs):
            pass

        def __class_getitem__(cls, item):
            return cls

    class _CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        def __class_getitem__(cls, item):
            return cls

    update.DataUpdateCoordinator = _Coordinator
    update.UpdateFailed = Exception
    update.CoordinatorEntity = _CoordinatorEntity
    module("homeassistant.helpers.device_registry").DeviceInfo = dict
    module("homeassistant.helpers.entity_platform").AddEntitiesCallback = object
    module("homeassistant.util")
    module("homeassistant.util.dt").now = lambda: None

    # Platform modules imported by entity files — only needed if those load.
    for name in (
        "homeassistant.components",
        "homeassistant.components.climate",
        "homeassistant.components.switch",
        "homeassistant.components.sensor",
        "homeassistant.components.number",
        "homeassistant.components.light",
        "homeassistant.components.binary_sensor",
    ):
        module(name)


_ensure_homeassistant_stubs()
