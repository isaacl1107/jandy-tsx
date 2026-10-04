"""Switch entities for Jandy TCX."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import TcxCoordinator
from .entity import TcxEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TcxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TcxPumpSwitch(coordinator),
            TcxHeaterSwitch(coordinator),
            TcxScheduleSwitch(coordinator),
        ]
    )


class TcxPumpSwitch(TcxEntity, SwitchEntity):
    _attr_name = "Filter pump"
    _attr_translation_key = "filter_pump"
    _attr_icon = "mdi:pump"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "filter_pump")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.pump_on

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.client.async_set_filter_pump(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.client.async_set_filter_pump(False)
        await self.coordinator.async_request_refresh()


class TcxHeaterSwitch(TcxEntity, SwitchEntity):
    _attr_name = "Heater enable"
    _attr_translation_key = "heater_enable"
    _attr_icon = "mdi:fire"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "heater_enable")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.heater_enabled

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.client.async_set_heater_enabled(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.client.async_set_heater_enabled(False)
        await self.coordinator.async_request_refresh()


class TcxScheduleSwitch(TcxEntity, SwitchEntity):
    """Master enable for the integration's local schedules."""

    _attr_name = "Auto schedule"
    _attr_translation_key = "auto_schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "auto_schedule")

    @property
    def is_on(self) -> bool:
        return self.coordinator.schedule_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.schedule_enabled = True
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.schedule_enabled = False
        self.async_write_ha_state()
