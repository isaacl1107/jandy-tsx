"""Tests for legacy default schedule migration on setup."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from custom_components.jandy_tcx import _migrate_legacy_default_schedules
from custom_components.jandy_tcx.const import CONF_SCHEDULES


def _entry(
    *,
    data_schedules: list[dict[str, Any]] | None = None,
    options_schedules: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    data: dict[str, Any] = {"email": "a@b.c", "serial": "X"}
    options: dict[str, Any] = {}
    if data_schedules is not None:
        data[CONF_SCHEDULES] = data_schedules
    if options_schedules is not None:
        options[CONF_SCHEDULES] = options_schedules
    return SimpleNamespace(
        entry_id="test-entry",
        data=data,
        options=options,
    )


def test_migrate_strips_legacy_from_data_and_options():
    stock = [
        {
            "id": "weekday-heat",
            "target": "heater",
            "days": [0, 1, 2, 3, 4],
            "start": "10:00",
            "end": "18:00",
            "enabled": True,
            "setpoint_f": 84,
        },
        {
            "id": "weekend-heat",
            "target": "heater",
            "days": [5, 6],
            "start": "09:00",
            "end": "20:00",
            "enabled": True,
            "setpoint_f": 86,
        },
        {
            "id": "daily-filter",
            "target": "pump",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "08:00",
            "end": "12:00",
            "enabled": True,
        },
    ]
    custom = {
        "id": "mine",
        "target": "light",
        "days": [5, 6],
        "start": "19:00",
        "end": "23:00",
        "enabled": True,
    }
    entry = _entry(
        data_schedules=list(stock),
        options_schedules=[*stock, custom],
    )
    hass = MagicMock()
    updates: list[dict[str, Any]] = []

    def _update(_entry, **kwargs):
        updates.append(kwargs)

    hass.config_entries.async_update_entry = _update

    _migrate_legacy_default_schedules(hass, entry)

    assert len(updates) == 1
    assert updates[0]["data"][CONF_SCHEDULES] == []
    assert updates[0]["options"][CONF_SCHEDULES] == [custom]


def test_migrate_noop_when_no_legacy():
    entry = _entry(
        options_schedules=[
            {
                "id": "mine",
                "target": "pump",
                "days": [0],
                "start": "08:00",
                "end": "12:00",
                "enabled": True,
            }
        ]
    )
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    _migrate_legacy_default_schedules(hass, entry)
    hass.config_entries.async_update_entry.assert_not_called()
