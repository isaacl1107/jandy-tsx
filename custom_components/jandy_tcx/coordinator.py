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
from .schedule import desired_states, managed_targets, slots_from_config

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
        # True only after HA schedules send filtration ON. Manual / panel ON
        # leaves this False so schedule end will not force the pump off.
        self._schedule_owns_pump = False
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
        # Push WS Authorization / StateStreamer deltas into HA immediately
        # so pump/temps from pib0/filt/ecm don't wait for the next REST poll.
        self.client.add_listener(self._on_client_state)

    def _on_client_state(self, state: TcxState) -> None:
        """Called from the WS receive loop when reported state changes."""
        if self.hass is None:
            return

        def _apply() -> None:
            # Avoid stomping an in-flight coordinator refresh.
            refresh = getattr(self, "_refresh_task", None)
            if refresh is not None and not refresh.done():
                return
            self.async_set_updated_data(state)

        self.hass.loop.call_soon_threadsafe(_apply)

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
                    await self.client.async_connect_ws(wait_for_auth=True)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "WS connect deferred; using REST shadow", exc_info=True
                    )
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
        managed = managed_targets(slots)

        # Only control equipment that has its own schedule. Otherwise a heater
        # window would force the pool light / water feature off every poll.
        #
        # Pump OFF is ownership-gated: only send filtration OFF when HA itself
        # turned the pump on for this schedule window. Manual ON / panel ON
        # never sets ownership, so idle-window refresh cannot yank them off.
        pump_managed = (
            "pump" in managed
            or "heater" in managed
            or "water_feature" in managed
        )
        if pump_managed:
            await self._maybe_set_pump(bool(desired["pump"]["on"]), state, now)

        if "heater" in managed:
            await self._maybe_set_heater(desired["heater"], state, now)
        if "light" in managed:
            await self._maybe_set_light(desired["light"]["on"], state, now)
        if "water_feature" in managed:
            await self._maybe_set_water_feature(
                desired["water_feature"]["on"], state, now
            )

    def mark_manual_pump(self, on: bool) -> None:
        """User toggle — HA no longer owns this pump run for schedule OFF."""
        self._schedule_owns_pump = False
        self._last_applied["pump"] = ("pump", "manual", bool(on))

    async def _maybe_set_pump(
        self, should_on: bool, state: TcxState, when: datetime
    ) -> None:
        if should_on:
            key = ("pump", True)
            if state.pump_on:
                # Already running (manual, panel, or prior schedule ON). Do not
                # claim ownership unless we already own this run.
                self._last_applied["pump"] = key
                return

            _LOGGER.info("Schedule enabling filter pump at %s", when.isoformat())
            await self.client.async_set_filter_pump(True)
            self._schedule_owns_pump = True
            self._last_applied["pump"] = key
            return

        # Window idle — only OFF if HA started this pump run.
        key = ("pump", False)
        if not self._schedule_owns_pump:
            _LOGGER.debug(
                "Ignoring schedule pump-off at %s (HA did not start this run)",
                when.isoformat(),
            )
            self._last_applied["pump"] = key
            return
        if not state.pump_on:
            self._schedule_owns_pump = False
            self._last_applied["pump"] = key
            return

        _LOGGER.info(
            "Schedule disabling filter pump at %s (HA started this run)",
            when.isoformat(),
        )
        await self.client.async_set_filter_pump(False)
        self._schedule_owns_pump = False
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

    async def _maybe_set_water_feature(
        self, should_on: bool, state: TcxState, when: datetime
    ) -> None:
        if not state.water_feature_available and state.water_feature_key is None:
            # Still allow schedules when aux key was inferred from mock/config.
            pass
        key = ("water_feature", should_on)
        if (
            self._last_applied.get("water_feature") == key
            and state.water_feature_on == should_on
        ):
            return
        if state.water_feature_on == should_on:
            self._last_applied["water_feature"] = key
            return
        _LOGGER.info(
            "Schedule %s water feature at %s",
            "enabling" if should_on else "disabling",
            when.isoformat(),
        )
        await self.client.async_set_water_feature(should_on)
        self._last_applied["water_feature"] = key
