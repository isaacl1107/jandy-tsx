"""Switch entities for Jandy TCX."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
        # Always register so the control appears once the panel reports a
        # waterfall / aux-pump circuit (including late WS discovery).
        TcxWaterFeatureSwitch(coordinator),
    ]
    known_aux: set[str] = set()

    def _new_aux_entities() -> list[SwitchEntity]:
        data = coordinator.data
        if not data:
            return []
        created: list[SwitchEntity] = []
        for key, circ in data.aux_circuits.items():
            if key in known_aux:
                continue
            if circ.get("kind") == "water_feature":
                # Covered by TcxWaterFeatureSwitch.
                known_aux.add(key)
                continue
            if key == data.light_key:
                continue
            created.append(TcxAuxSwitch(coordinator, key))
            known_aux.add(key)
        return created

    entities.extend(_new_aux_entities())
    async_add_entities(entities)

    @callback
    def _on_coordinator_update() -> None:
        late = _new_aux_entities()
        if late:
            async_add_entities(late)

    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))


class TcxPumpSwitch(TcxEntity, SwitchEntity):
    _attr_name = "Filter pump"
    _attr_translation_key = "filter_pump"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "filter_pump")

    @property
    def icon(self) -> str:
        return "mdi:pump" if self.is_on else "mdi:pump-off"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.pump_on

    async def async_turn_on(self, **kwargs) -> None:
        # Mark manual before refresh so an idle schedule cannot immediately
        # force the pump back off (seen as On→Off in ~6s).
        self.coordinator.mark_manual("pump", True)
        await self.coordinator.client.async_set_filter_pump(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.mark_manual("pump", False)
        await self.coordinator.client.async_set_filter_pump(False)
        await self.coordinator.async_request_refresh()


class TcxHeaterSwitch(TcxEntity, SwitchEntity):
    _attr_name = "Heater enable"
    _attr_translation_key = "heater_enable"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "heater_enable")

    @property
    def icon(self) -> str:
        return "mdi:fire" if self.is_on else "mdi:fire-off"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.heater_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.mark_manual("heater", True)
        await self.coordinator.client.async_set_heater_enabled(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.mark_manual("heater", False)
        await self.coordinator.client.async_set_heater_enabled(False)
        await self.coordinator.async_request_refresh()


class TcxScheduleSwitch(TcxEntity, SwitchEntity):
    """Master enable for the integration's local schedules."""

    _attr_name = "Auto schedule"
    _attr_translation_key = "auto_schedule"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "auto_schedule")

    @property
    def icon(self) -> str:
        return "mdi:calendar-clock" if self.is_on else "mdi:calendar-remove"

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
    _attr_name = "Water feature"

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "water_feature")

    @property
    def name(self) -> str:
        data = self.coordinator.data
        if data and data.water_feature_name:
            return data.water_feature_name
        return "Water feature"

    @property
    def icon(self) -> str:
        return "mdi:fountain" if self.is_on else "mdi:water-off"

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return bool(
            self.coordinator.last_update_success
            and data
            and (data.water_feature_available or data.water_feature_key)
        )

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return bool(data and data.water_feature_on)

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        data = self.coordinator.data
        if not data:
            return {}
        return {"aux_key": data.water_feature_key}

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.mark_manual("water_feature", True)
        await self.coordinator.client.async_set_water_feature(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.mark_manual("water_feature", False)
        await self.coordinator.client.async_set_water_feature(False)
        await self.coordinator.async_request_refresh()


class TcxAuxSwitch(TcxEntity, SwitchEntity):
    """Generic non-light aux relay (aux pump, blower, etc.)."""

    _attr_translation_key = "aux_circuit"

    def __init__(self, coordinator: TcxCoordinator, aux_key: str) -> None:
        super().__init__(coordinator, f"aux_{aux_key}")
        self._aux_key = aux_key
        self._attr_name = aux_key

    def _circuit(self) -> dict | None:
        data = self.coordinator.data
        if not data:
            return None
        return data.aux_circuits.get(self._aux_key)

    @property
    def name(self) -> str:
        circ = self._circuit()
        if circ and circ.get("name"):
            return str(circ["name"])
        return self._aux_key

    @property
    def icon(self) -> str:
        return (
            "mdi:electric-switch-closed" if self.is_on else "mdi:electric-switch"
        )

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success and self._circuit())

    @property
    def is_on(self) -> bool:
        circ = self._circuit()
        return bool(circ and circ.get("on"))

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        circ = self._circuit() or {}
        return {
            "aux_key": self._aux_key,
            "app": circ.get("app"),
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.client.async_set_aux(self._aux_key, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.client.async_set_aux(self._aux_key, False)
        await self.coordinator.async_request_refresh()
