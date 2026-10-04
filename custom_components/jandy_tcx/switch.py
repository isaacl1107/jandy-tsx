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
    entities: list[SwitchEntity] = [
        TcxPumpSwitch(coordinator),
        TcxHeaterSwitch(coordinator),
        TcxScheduleSwitch(coordinator),
    ]
    # Water feature appears when TCX reports a WF aux circuit.
    if (
        coordinator.data
        and (
            coordinator.data.water_feature_available
            or coordinator.data.water_feature_key
        )
    ):
        entities.append(TcxWaterFeatureSwitch(coordinator))
    async_add_entities(entities)


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
        # Mark manual before refresh so an idle pump schedule cannot
        # immediately force the pump back off (seen as On→Off in ~6s).
        self.coordinator.mark_manual_pump(True)
        await self.coordinator.client.async_set_filter_pump(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.mark_manual_pump(False)
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


class TcxWaterFeatureSwitch(TcxEntity, SwitchEntity):
    _attr_translation_key = "water_feature"
    _attr_icon = "mdi:fountain"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "water_feature")
        self._attr_name = coordinator.data.water_feature_name or "Water feature"

    @property
    def available(self) -> bool:
        return bool(
            self.coordinator.last_update_success
            and (
                self.coordinator.data.water_feature_available
                or self.coordinator.data.water_feature_key
            )
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.water_feature_on

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.client.async_set_water_feature(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.client.async_set_water_feature(False)
        await self.coordinator.async_request_refresh()
