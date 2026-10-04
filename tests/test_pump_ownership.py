"""Pump schedule OFF is gated on HA having started the run."""

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
    # DataUpdateCoordinator stub ignores args.
    coord = TcxCoordinator(MagicMock(), entry, client)
    return coord


@pytest.mark.asyncio
async def test_schedule_on_claims_ownership_and_off_when_idle():
    coord = _coordinator()
    when = datetime(2026, 10, 5, 10, 0)

    await coord._maybe_set_pump(True, TcxState(pump_on=False), when)
    coord.client.async_set_filter_pump.assert_awaited_with(True)
    assert coord._schedule_owns_pump is True

    coord.client.async_set_filter_pump.reset_mock()
    await coord._maybe_set_pump(False, TcxState(pump_on=True), when)
    coord.client.async_set_filter_pump.assert_awaited_with(False)
    assert coord._schedule_owns_pump is False


@pytest.mark.asyncio
async def test_already_on_does_not_claim_ownership_so_idle_skips_off():
    coord = _coordinator()
    when = datetime(2026, 10, 5, 10, 0)

    await coord._maybe_set_pump(True, TcxState(pump_on=True), when)
    coord.client.async_set_filter_pump.assert_not_awaited()
    assert coord._schedule_owns_pump is False

    await coord._maybe_set_pump(False, TcxState(pump_on=True), when)
    coord.client.async_set_filter_pump.assert_not_awaited()
    assert coord._schedule_owns_pump is False


@pytest.mark.asyncio
async def test_manual_on_clears_ownership_so_schedule_end_skips_off():
    coord = _coordinator()
    when = datetime(2026, 10, 5, 10, 0)

    await coord._maybe_set_pump(True, TcxState(pump_on=False), when)
    assert coord._schedule_owns_pump is True

    coord.mark_manual_pump(True)
    assert coord._schedule_owns_pump is False

    coord.client.async_set_filter_pump.reset_mock()
    await coord._maybe_set_pump(False, TcxState(pump_on=True), when)
    coord.client.async_set_filter_pump.assert_not_awaited()
