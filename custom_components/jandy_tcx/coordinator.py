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
        if "pump" in managed:
            await self._maybe_set_pump(desired["pump"]["on"], state, now)
        elif desired["pump"]["on"] and (
            "heater" in managed or "water_feature" in managed
        ):
            # Safety interlock: force pump on while heater/WF schedule is active,
            # but never force the pump off when it is not itself scheduled.
            await self._maybe_set_pump(True, state, now)

        if "heater" in managed:
            await self._maybe_set_heater(desired["heater"], state, now)
        if "light" in managed:
            await self._maybe_set_light(desired["light"]["on"], state, now)
        if "water_feature" in managed:
            await self._maybe_set_water_feature(
                desired["water_feature"]["on"], state, now
            )

    def mark_manual_pump(self, on: bool) -> None:
        """Record a user toggle so idle schedules do not immediately fight it."""
        self._last_applied["pump"] = ("pump", "manual", bool(on))

    async def _maybe_set_pump(
        self, should_on: bool, state: TcxState, when: datetime
    ) -> None:
        key = ("pump", should_on)
        prev = self._last_applied.get("pump")
        if prev == key and state.pump_on == should_on:
            return
        if state.pump_on == should_on:
            # Keep a prior manual marker so an idle schedule cannot force-off
            # a pump the user just enabled outside the schedule window.
            if not (
                isinstance(prev, tuple)
                and len(prev) == 3
                and prev[0] == "pump"
                and prev[1] == "manual"
            ):
                self._last_applied["pump"] = key
            return

        # Outside an active pump window, desired is False. Only auto-off when
        # *this* schedule previously turned the pump on — never yank a manual
        # ON (activity log showed On→2500 RPM→Off within ~6s from that fight).
        if not should_on and prev != ("pump", True):
            _LOGGER.debug(
                "Skipping schedule pump-off at %s (prev=%s manual_or_idle)",
                when.isoformat(),
                prev,
            )
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
