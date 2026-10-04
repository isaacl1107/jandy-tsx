"""Sensor entities for Jandy TCX."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import ATTR_NEXT_OFF, ATTR_NEXT_ON, ATTR_SCHEDULE_ACTIVE, DOMAIN
from .coordinator import TcxCoordinator
from .entity import TcxEntity
from .schedule import desired_states, next_transition, slots_from_config


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TcxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TcxTemperatureSensor(coordinator, "water", "Pool temperature"),
            TcxTemperatureSensor(coordinator, "air", "Air temperature"),
            TcxSwcSensor(coordinator),
            TcxScheduleSensor(coordinator, "heater"),
            TcxScheduleSensor(coordinator, "pump"),
        ]
    )


class TcxTemperatureSensor(TcxEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(
        self, coordinator: TcxCoordinator, kind: str, name: str
    ) -> None:
        super().__init__(coordinator, f"{kind}_temperature")
        self._kind = kind
        self._attr_name = name

    @property
    def native_value(self) -> float | None:
        if self._kind == "water":
            return self.coordinator.data.water_temp_f
        return self.coordinator.data.air_temp_f


class TcxSwcSensor(TcxEntity, SensorEntity):
    _attr_name = "Chlorinator level"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-percent"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "swc_level")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.swc_percent


class TcxScheduleSensor(TcxEntity, SensorEntity):
    """Shows whether the local schedule wants heater/pump on."""

    _attr_icon = "mdi:calendar-check"

    def __init__(self, coordinator: TcxCoordinator, target: str) -> None:
        super().__init__(coordinator, f"schedule_{target}")
        self._target = target
        self._attr_name = f"{target.title()} schedule"

    @property
    def native_value(self) -> str:
        slots = slots_from_config(self.coordinator.schedules_raw)
        now = dt_util.now()
        desired = desired_states(slots, now)
        active = bool(desired[self._target]["on"])  # type: ignore[index]
        if not self.coordinator.schedule_enabled:
            return "paused"
        return "active" if active else "idle"

    @property
    def extra_state_attributes(self) -> dict:
        slots = slots_from_config(self.coordinator.schedules_raw)
        now = dt_util.now()
        desired = desired_states(slots, now)
        active = bool(desired[self._target]["on"])  # type: ignore[index]
        nxt_on = next_transition(slots, self._target, now, looking_for=True)  # type: ignore[arg-type]
        nxt_off = next_transition(slots, self._target, now, looking_for=False)  # type: ignore[arg-type]
        return {
            ATTR_SCHEDULE_ACTIVE: active,
            ATTR_NEXT_ON: nxt_on.isoformat() if nxt_on else None,
            ATTR_NEXT_OFF: nxt_off.isoformat() if nxt_off else None,
            "schedule_enabled": self.coordinator.schedule_enabled,
            "slots": [
                slot
                for slot in self.coordinator.schedules_raw
                if slot.get("target") == self._target
            ],
        }
