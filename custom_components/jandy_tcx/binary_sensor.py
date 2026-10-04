"""Binary sensors for Jandy TCX."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
            TcxOnlineSensor(coordinator),
            TcxHeaterRunningSensor(coordinator),
        ]
    )


class TcxOnlineSensor(TcxEntity, BinarySensorEntity):
    _attr_name = "Cloud connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "online")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.online


class TcxHeaterRunningSensor(TcxEntity, BinarySensorEntity):
    _attr_name = "Heater running"
    _attr_device_class = BinarySensorDeviceClass.HEAT
    _attr_icon = "mdi:fire"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "heater_running")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.heater_running
