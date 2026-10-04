"""Number entities for Jandy TCX."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
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
    async_add_entities(
        [
            TcxSetpointNumber(coordinator),
            TcxPumpRpmNumber(coordinator),
        ]
    )


class TcxSetpointNumber(TcxEntity, NumberEntity):
    _attr_name = "Heater setpoint"
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_native_min_value = TEMP_MIN_F
    _attr_native_max_value = TEMP_MAX_F
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "heater_setpoint")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.heater_setpoint_f

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.client.async_set_heater_setpoint(value)
        await self.coordinator.async_request_refresh()


class TcxPumpRpmNumber(TcxEntity, NumberEntity):
    _attr_name = "Pump RPM"
    _attr_native_step = 50
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:speedometer"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "pump_rpm")

    @property
    def native_min_value(self) -> float:
        return float(self.coordinator.data.pump_min_rpm)

    @property
    def native_max_value(self) -> float:
        return float(self.coordinator.data.pump_max_rpm)

    @property
    def native_value(self) -> float | None:
        return (
            float(self.coordinator.data.pump_rpm)
            if self.coordinator.data.pump_rpm is not None
            else None
        )

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.client.async_set_pump_rpm(int(value))
        await self.coordinator.async_request_refresh()
