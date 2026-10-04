"""Jandy AquaLink TCX custom integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TcxClient
from .const import (
    CONF_EMAIL,
    CONF_MOCK,
    CONF_PASSWORD,
    CONF_SERIAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import TcxCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS_TYPED = [Platform(p) for p in PLATFORMS]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TCX from a config entry."""
    session = async_get_clientsession(hass)
    client = TcxClient(
        session,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        serial=entry.data[CONF_SERIAL],
        mock=bool(entry.data.get(CONF_MOCK, False)),
    )
    await client.async_login()
    if not client.mock:
        await client.async_get_shadow()

    coordinator = TcxCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_TYPED)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    async def handle_set_schedule_enabled(call: ServiceCall) -> None:
        coordinator.schedule_enabled = bool(call.data.get("enabled", True))
        await coordinator.async_request_refresh()

    async def handle_apply_schedules_now(call: ServiceCall) -> None:
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, "set_schedule_enabled", handle_set_schedule_enabled
    )
    hass.services.async_register(
        DOMAIN, "apply_schedules_now", handle_apply_schedules_now
    )
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS_TYPED
    )
    if unload_ok:
        coordinator: TcxCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.client.async_close()
    return unload_ok
