"""Climate entity for the TCX pool heater."""

from __future__ import annotations

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, TEMP_MAX_F, TEMP_MIN_F
from .coordinator import TcxCoordinator
from .entity import TcxEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TcxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TcxHeaterClimate(coordinator)])


class TcxHeaterClimate(TcxEntity, ClimateEntity):
    """Pool heater as a climate entity."""

    _attr_name = "Heater"
    _attr_translation_key = "heater"
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_min_temp = TEMP_MIN_F
    _attr_max_temp = TEMP_MAX_F
    _attr_target_temperature_step = 1.0

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "heater_climate")

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.data.water_temp_f

    @property
    def target_temperature(self) -> float | None:
        return self.coordinator.data.heater_setpoint_f

    @property
    def hvac_mode(self) -> HVACMode:
        return (
            HVACMode.HEAT
            if self.coordinator.data.heater_enabled
            else HVACMode.OFF
        )

    @property
    def hvac_action(self) -> HVACAction:
        if not self.coordinator.data.heater_enabled:
            return HVACAction.OFF
        if self.coordinator.data.heater_running:
            return HVACAction.HEATING
        return HVACAction.IDLE

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        enabled = hvac_mode == HVACMode.HEAT
        self.coordinator.mark_manual("heater", enabled)
        await self.coordinator.client.async_set_heater_enabled(enabled)
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        await self.coordinator.client.async_set_heater_setpoint(float(temp))
        await self.coordinator.async_request_refresh()
