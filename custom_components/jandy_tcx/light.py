"""Pool light entity for Jandy TCX."""

from __future__ import annotations

from homeassistant.components.light import (
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import TcxCoordinator
from .entity import TcxEntity

# Common Jandy WaterColors program indexes used by iAquaLink.
JANDY_EFFECTS = [
    "Alpine White",
    "Sky Blue",
    "Cobalt Blue",
    "Caribbean Blue",
    "Spring Green",
    "Emerald Green",
    "Emerald Rose",
    "Magenta",
    "Violet",
    "Slow Color Splash",
    "Fast Color Splash",
    "America",
    "Fat Tuesday",
    "Disco Party",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TcxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TcxPoolLight(coordinator)])


class TcxPoolLight(TcxEntity, LightEntity):
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = JANDY_EFFECTS

    def __init__(self, coordinator: TcxCoordinator) -> None:
        super().__init__(coordinator, "pool_light")
        self._attr_name = coordinator.data.light_name or "Pool light"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.light_on

    @property
    def effect(self) -> str | None:
        idx = self.coordinator.data.light_color
        if 0 <= idx < len(JANDY_EFFECTS):
            return JANDY_EFFECTS[idx]
        return None

    async def async_turn_on(self, **kwargs) -> None:
        color = None
        effect = kwargs.get(ATTR_EFFECT)
        if effect and effect in JANDY_EFFECTS:
            color = JANDY_EFFECTS.index(effect)
        await self.coordinator.client.async_set_light(True, color)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.client.async_set_light(False)
        await self.coordinator.async_request_refresh()
