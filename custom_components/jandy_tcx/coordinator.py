"""DataUpdateCoordinator for Jandy TCX."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TcxApiError, TcxAuthError, TcxClient, TcxState
from .const import CONF_POLL_INTERVAL, CONF_SCHEDULES, DEFAULT_POLL_INTERVAL, DOMAIN
from .schedule import desired_states, slots_from_config

_LOGGER = logging.getLogger(__name__)


class TcxCoordinator(DataUpdateCoordinator[TcxState]):
    """Polls TCX state and applies local heater/pump schedules."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: TcxClient,
    ) -> None:
        self.entry = entry
        self.client = client
        self.schedule_enabled = True
        self._last_applied: dict[str, Any] = {}
        poll = entry.options.get(
            CONF_POLL_INTERVAL,
            entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(15, int(poll))),
        )

    @property
    def schedules_raw(self) -> list[dict[str, Any]]:
        return list(
            self.entry.options.get(
                CONF_SCHEDULES,
                self.entry.data.get(CONF_SCHEDULES, []),
            )
        )

    async def _async_update_data(self) -> TcxState:
        try:
            if not self.client.mock:
                try:
                    await self.client.async_connect_ws()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("WS connect deferred; using REST shadow", exc_info=True)
                await self.client.async_get_shadow()
            state = self.client.get_state()
            await self._async_apply_schedules(state)
            return self.client.get_state()
        except TcxAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except TcxApiError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_apply_schedules(self, state: TcxState) -> None:
        if not self.schedule_enabled:
            return
        slots = slots_from_config(self.schedules_raw)
        if not slots:
            return
        now = dt_util.now()
        desired = desired_states(slots, now)
        await self._maybe_set_pump(desired["pump"]["on"], state, now)
        await self._maybe_set_heater(desired["heater"], state, now)
        await self._maybe_set_light(desired["light"]["on"], state, now)

    async def _maybe_set_pump(
        self, should_on: bool, state: TcxState, when: datetime
    ) -> None:
        key = ("pump", should_on)
        if self._last_applied.get("pump") == key and state.pump_on == should_on:
            return
        if state.pump_on == should_on:
            self._last_applied["pump"] = key
            return
        _LOGGER.info(
            "Schedule %s filter pump at %s",
            "enabling" if should_on else "disabling",
            when.isoformat(),
        )
        await self.client.async_set_filter_pump(should_on)
        self._last_applied["pump"] = key

    async def _maybe_set_heater(
        self, heater: dict[str, Any], state: TcxState, when: datetime
    ) -> None:
        should_on = bool(heater["on"])
        setpoint = heater.get("setpoint_f")
        key = ("heater", should_on, setpoint)
        if self._last_applied.get("heater") == key:
            if state.heater_enabled == should_on and (
                setpoint is None
                or state.heater_setpoint_f is None
                or abs(state.heater_setpoint_f - float(setpoint)) < 0.2
            ):
                return

        if setpoint is not None and (
            state.heater_setpoint_f is None
            or abs(state.heater_setpoint_f - float(setpoint)) >= 0.2
        ):
            _LOGGER.info(
                "Schedule setting heater setpoint to %s°F at %s",
                setpoint,
                when.isoformat(),
            )
            await self.client.async_set_heater_setpoint(float(setpoint))

        if state.heater_enabled != should_on:
            _LOGGER.info(
                "Schedule %s heater at %s",
                "enabling" if should_on else "disabling",
                when.isoformat(),
            )
            await self.client.async_set_heater_enabled(should_on)

        self._last_applied["heater"] = key

    async def _maybe_set_light(
        self, should_on: bool, state: TcxState, when: datetime
    ) -> None:
        key = ("light", should_on)
        if self._last_applied.get("light") == key and state.light_on == should_on:
            return
        if state.light_on == should_on:
            self._last_applied["light"] = key
            return
        _LOGGER.info(
            "Schedule %s pool light at %s",
            "enabling" if should_on else "disabling",
            when.isoformat(),
        )
        await self.client.async_set_light(should_on)
        self._last_applied["light"] = key
