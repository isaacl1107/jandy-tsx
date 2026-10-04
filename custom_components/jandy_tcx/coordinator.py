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

_OWNABLE_TARGETS = ("pump", "heater", "light", "water_feature")


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
        # True only after HA schedules send ON for that target. Manual / panel
        # ON leaves the flag False so schedule end will not force OFF.
        self._schedule_owns: dict[str, bool] = {
            target: False for target in _OWNABLE_TARGETS
        }
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
        # OFF is ownership-gated for every on/off target: only send OFF when HA
        # itself turned that equipment on for this schedule window.
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

    def mark_manual(self, target: str, on: bool) -> None:
        """User toggle — HA no longer owns this run for schedule OFF."""
        if target in self._schedule_owns:
            self._schedule_owns[target] = False
        self._last_applied[target] = (target, "manual", bool(on))

    def mark_manual_pump(self, on: bool) -> None:
        """Compatibility wrapper for the filter-pump switch."""
        self.mark_manual("pump", on)

    async def _maybe_set_pump(
        self, should_on: bool, state: TcxState, when: datetime
    ) -> None:
        if should_on:
            key = ("pump", True)
            if state.pump_on:
                # Already running — do not claim ownership.
                self._last_applied["pump"] = key
                return

            _LOGGER.info("Schedule enabling filter pump at %s", when.isoformat())
            await self.client.async_set_filter_pump(True)
            self._schedule_owns["pump"] = True
            self._last_applied["pump"] = key
            return

        key = ("pump", False)
        if not self._schedule_owns.get("pump"):
            _LOGGER.debug(
                "Ignoring schedule pump-off at %s (HA did not start this run)",
                when.isoformat(),
            )
            self._last_applied["pump"] = key
            return
        if not state.pump_on:
            self._schedule_owns["pump"] = False
            self._last_applied["pump"] = key
            return

        _LOGGER.info(
            "Schedule disabling filter pump at %s (HA started this run)",
            when.isoformat(),
        )
        await self.client.async_set_filter_pump(False)
        self._schedule_owns["pump"] = False
        self._last_applied["pump"] = key

    async def _maybe_set_heater(
        self, heater: dict[str, Any], state: TcxState, when: datetime
    ) -> None:
        should_on = bool(heater["on"])
        setpoint = heater.get("setpoint_f")

        if should_on:
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

            key = ("heater", True, setpoint)
            if state.heater_enabled:
                self._last_applied["heater"] = key
                return

            _LOGGER.info("Schedule enabling heater at %s", when.isoformat())
            await self.client.async_set_heater_enabled(True)
            self._schedule_owns["heater"] = True
            self._last_applied["heater"] = key
            return

        key = ("heater", False, None)
        if not self._schedule_owns.get("heater"):
            _LOGGER.debug(
                "Ignoring schedule heater-off at %s (HA did not start this run)",
                when.isoformat(),
            )
            self._last_applied["heater"] = key
            return
        if not state.heater_enabled:
            self._schedule_owns["heater"] = False
            self._last_applied["heater"] = key
            return

        _LOGGER.info(
            "Schedule disabling heater at %s (HA started this run)",
            when.isoformat(),
        )
        await self.client.async_set_heater_enabled(False)
        self._schedule_owns["heater"] = False
        self._last_applied["heater"] = key

    async def _maybe_set_light(
        self, should_on: bool, state: TcxState, when: datetime
    ) -> None:
        if should_on:
            key = ("light", True)
            if state.light_on:
                self._last_applied["light"] = key
                return

            _LOGGER.info("Schedule enabling pool light at %s", when.isoformat())
            await self.client.async_set_light(True)
            self._schedule_owns["light"] = True
            self._last_applied["light"] = key
            return

        key = ("light", False)
        if not self._schedule_owns.get("light"):
            _LOGGER.debug(
                "Ignoring schedule light-off at %s (HA did not start this run)",
                when.isoformat(),
            )
            self._last_applied["light"] = key
            return
        if not state.light_on:
            self._schedule_owns["light"] = False
            self._last_applied["light"] = key
            return

        _LOGGER.info(
            "Schedule disabling pool light at %s (HA started this run)",
            when.isoformat(),
        )
        await self.client.async_set_light(False)
        self._schedule_owns["light"] = False
        self._last_applied["light"] = key

    async def _maybe_set_water_feature(
        self, should_on: bool, state: TcxState, when: datetime
    ) -> None:
        if should_on:
            key = ("water_feature", True)
            if state.water_feature_on:
                self._last_applied["water_feature"] = key
                return

            _LOGGER.info("Schedule enabling water feature at %s", when.isoformat())
            await self.client.async_set_water_feature(True)
            self._schedule_owns["water_feature"] = True
            self._last_applied["water_feature"] = key
            return

        key = ("water_feature", False)
        if not self._schedule_owns.get("water_feature"):
            _LOGGER.debug(
                "Ignoring schedule water-feature-off at %s "
                "(HA did not start this run)",
                when.isoformat(),
            )
            self._last_applied["water_feature"] = key
            return
        if not state.water_feature_on:
            self._schedule_owns["water_feature"] = False
            self._last_applied["water_feature"] = key
            return

        _LOGGER.info(
            "Schedule disabling water feature at %s (HA started this run)",
            when.isoformat(),
        )
        await self.client.async_set_water_feature(False)
        self._schedule_owns["water_feature"] = False
        self._last_applied["water_feature"] = key
