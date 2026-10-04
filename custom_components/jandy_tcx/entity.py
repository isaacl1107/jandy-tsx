"""Shared entity base for Jandy TCX."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TcxCoordinator


class TcxEntity(CoordinatorEntity[TcxCoordinator]):
    """Base entity tied to one TCX controller."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TcxCoordinator, key: str) -> None:
        super().__init__(coordinator)
        serial = coordinator.client.serial
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=coordinator.data.name if coordinator.data else f"TCX {serial}",
            manufacturer="Jandy",
            model="AquaLink TCX",
            serial_number=serial,
        )
