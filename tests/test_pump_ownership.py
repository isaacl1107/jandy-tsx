"""Schedule OFF is gated on HA having started each equipment run."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.jandy_tcx.api import TcxState
from custom_components.jandy_tcx.coordinator import TcxCoordinator


def _coordinator() -> TcxCoordinator:
    entry = SimpleNamespace(
        data={"poll_interval": 30, "schedules": []},
        options={},
    )
    client = MagicMock()
    client.add_listener = MagicMock()
    client.async_set_filter_pump = AsyncMock()
    client.async_set_heater_enabled = AsyncMock()
    client.async_set_heater_setpoint = AsyncMock()
    client.async_set_light = AsyncMock()
    client.async_set_water_feature = AsyncMock()
    return TcxCoordinator(MagicMock(), entry, client)


@pytest.mark.asyncio
async def test_schedule_on_claims_ownership_and_off_when_idle():
    coord = _coordinator()
    when = datetime(2026, 10, 5, 10, 0)

    await coord._maybe_set_pump(True, TcxState(pump_on=False), when)
    coord.client.async_set_filter_pump.assert_awaited_with(True)
    assert coord._schedule_owns["pump"] is True

    coord.client.async_set_filter_pump.reset_mock()
    await coord._maybe_set_pump(False, TcxState(pump_on=True), when)
    coord.client.async_set_filter_pump.assert_awaited_with(False)
    assert coord._schedule_owns["pump"] is False


@pytest.mark.asyncio
async def test_already_on_does_not_claim_ownership_so_idle_skips_off():
    coord = _coordinator()
    when = datetime(2026, 10, 5, 10, 0)

    await coord._maybe_set_pump(True, TcxState(pump_on=True), when)
    coord.client.async_set_filter_pump.assert_not_awaited()
    assert coord._schedule_owns["pump"] is False

    await coord._maybe_set_pump(False, TcxState(pump_on=True), when)
    coord.client.async_set_filter_pump.assert_not_awaited()
    assert coord._schedule_owns["pump"] is False


@pytest.mark.asyncio
async def test_manual_on_clears_ownership_so_schedule_end_skips_off():
    coord = _coordinator()
    when = datetime(2026, 10, 5, 10, 0)

    await coord._maybe_set_pump(True, TcxState(pump_on=False), when)
    assert coord._schedule_owns["pump"] is True

    coord.mark_manual("pump", True)
    assert coord._schedule_owns["pump"] is False

    coord.client.async_set_filter_pump.reset_mock()
    await coord._maybe_set_pump(False, TcxState(pump_on=True), when)
    coord.client.async_set_filter_pump.assert_not_awaited()


@pytest.mark.asyncio
async def test_heater_ownership_gates_off_and_keeps_setpoint_updates():
    coord = _coordinator()
    when = datetime(2026, 10, 5, 10, 0)

    await coord._maybe_set_heater(
        {"on": True, "setpoint_f": 84},
        TcxState(heater_enabled=False, heater_setpoint_f=80),
        when,
    )
    coord.client.async_set_heater_setpoint.assert_awaited_with(84.0)
    coord.client.async_set_heater_enabled.assert_awaited_with(True)
    assert coord._schedule_owns["heater"] is True

    coord.client.async_set_heater_enabled.reset_mock()
    await coord._maybe_set_heater(
        {"on": False, "setpoint_f": 84},
        TcxState(heater_enabled=True, heater_setpoint_f=84),
        when,
    )
    coord.client.async_set_heater_enabled.assert_awaited_with(False)
    assert coord._schedule_owns["heater"] is False

    # Already on (manual/panel): setpoint may update, but no ownership / no OFF.
    coord.client.async_set_heater_enabled.reset_mock()
    coord.client.async_set_heater_setpoint.reset_mock()
    await coord._maybe_set_heater(
        {"on": True, "setpoint_f": 86},
        TcxState(heater_enabled=True, heater_setpoint_f=84),
        when,
    )
    coord.client.async_set_heater_setpoint.assert_awaited_with(86.0)
    coord.client.async_set_heater_enabled.assert_not_awaited()
    assert coord._schedule_owns["heater"] is False

    await coord._maybe_set_heater(
        {"on": False, "setpoint_f": None},
        TcxState(heater_enabled=True, heater_setpoint_f=86),
        when,
    )
    coord.client.async_set_heater_enabled.assert_not_awaited()


@pytest.mark.asyncio
async def test_light_and_water_feature_ownership():
    coord = _coordinator()
    when = datetime(2026, 10, 5, 20, 0)

    await coord._maybe_set_light(True, TcxState(light_on=False), when)
    coord.client.async_set_light.assert_awaited_with(True)
    assert coord._schedule_owns["light"] is True

    coord.mark_manual("light", True)
    coord.client.async_set_light.reset_mock()
    await coord._maybe_set_light(False, TcxState(light_on=True), when)
    coord.client.async_set_light.assert_not_awaited()

    await coord._maybe_set_water_feature(
        True, TcxState(water_feature_on=False), when
    )
    coord.client.async_set_water_feature.assert_awaited_with(True)
    assert coord._schedule_owns["water_feature"] is True

    coord.client.async_set_water_feature.reset_mock()
    await coord._maybe_set_water_feature(
        False, TcxState(water_feature_on=True), when
    )
    coord.client.async_set_water_feature.assert_awaited_with(False)
    assert coord._schedule_owns["water_feature"] is False
