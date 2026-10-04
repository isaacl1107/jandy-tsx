"""Jandy AquaLink TCX custom integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from homeassistant.exceptions import ConfigEntryNotReady

from .api import TcxApiError, TcxAuthError, TcxClient
from .const import (
    CONF_EMAIL,
    CONF_MOCK,
    CONF_PASSWORD,
    CONF_SCHEDULES,
    CONF_SERIAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import TcxCoordinator
from .schedule import strip_legacy_default_schedules

_LOGGER = logging.getLogger(__name__)

PLATFORMS_TYPED = [Platform(p) for p in PLATFORMS]


def _migrate_legacy_default_schedules(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove stock sample schedules seeded by early releases."""
    data = dict(entry.data)
    options = dict(entry.options)
    removed_total = 0
    update_kwargs: dict[str, Any] = {}

    if CONF_SCHEDULES in data:
        cleaned, removed = strip_legacy_default_schedules(
            list(data.get(CONF_SCHEDULES) or [])
        )
        if removed:
            data[CONF_SCHEDULES] = cleaned
            update_kwargs["data"] = data
            removed_total += removed

    if CONF_SCHEDULES in options:
        cleaned, removed = strip_legacy_default_schedules(
            list(options.get(CONF_SCHEDULES) or [])
        )
        if removed:
            options[CONF_SCHEDULES] = cleaned
            update_kwargs["options"] = options
            removed_total += removed

    if not update_kwargs:
        return

    hass.config_entries.async_update_entry(entry, **update_kwargs)
    _LOGGER.info(
        "Removed %s legacy default schedule(s) from TCX config entry %s",
        removed_total,
        entry.entry_id,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TCX from a config entry."""
    _migrate_legacy_default_schedules(hass, entry)
    session = async_get_clientsession(hass)
    client = TcxClient(
        session,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        serial=entry.data[CONF_SERIAL],
        mock=bool(entry.data.get(CONF_MOCK, False)),
    )
    try:
        await client.async_login()
        if not client.mock:
            # REST main shadow lacks filt0/ecm0/water (those arrive on WS).
            # Connect websocket first so the first HA refresh has pump + temps.
            try:
                await client.async_connect_ws(wait_for_auth=True)
            except Exception as ws_err:  # noqa: BLE001
                _LOGGER.warning(
                    "TCX websocket bootstrap failed (%s); falling back to REST",
                    ws_err,
                )
            try:
                await client.async_get_shadow()
            except TcxApiError as err:
                if not client._ws_auth_event.is_set():  # noqa: SLF001
                    raise ConfigEntryNotReady(
                        f"TCX cloud unavailable: {err}"
                    ) from err
                _LOGGER.warning(
                    "Initial TCX REST shadow failed (%s); using websocket state",
                    err,
                )
    except TcxAuthError as err:
        raise ConfigEntryNotReady(f"TCX authentication failed: {err}") from err

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
