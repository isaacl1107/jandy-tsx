"""Tests for weekly schedule helpers."""

from datetime import datetime, time

from custom_components.jandy_tcx.schedule import (
    ScheduleSlot,
    desired_states,
    next_transition,
    slots_from_config,
)


def test_weekday_heater_window():
    slots = [
        ScheduleSlot(
            id="h1",
            target="heater",
            days=(0, 1, 2, 3, 4),
            start=time(10, 0),
            end=time(18, 0),
            setpoint_f=84,
        )
    ]
    monday_noon = datetime(2026, 10, 5, 12, 0)  # Monday
    monday_evening = datetime(2026, 10, 5, 19, 0)
    saturday = datetime(2026, 10, 10, 12, 0)

    assert desired_states(slots, monday_noon)["heater"]["on"] is True
    assert desired_states(slots, monday_noon)["heater"]["setpoint_f"] == 84
    assert desired_states(slots, monday_noon)["pump"]["on"] is True  # interlock
    assert desired_states(slots, monday_evening)["heater"]["on"] is False
    assert desired_states(slots, saturday)["heater"]["on"] is False


def test_overnight_window():
    slots = [
        ScheduleSlot(
            id="night",
            target="pump",
            days=(0,),
            start=time(22, 0),
            end=time(6, 0),
        )
    ]
    assert desired_states(slots, datetime(2026, 10, 5, 23, 0))["pump"]["on"] is True
    assert desired_states(slots, datetime(2026, 10, 5, 5, 0))["pump"]["on"] is True
    assert desired_states(slots, datetime(2026, 10, 5, 12, 0))["pump"]["on"] is False


def test_slots_from_config_and_next_transition():
    raw = [
        {
            "id": "h1",
            "target": "heater",
            "days": [0],
            "start": "10:00",
            "end": "18:00",
            "enabled": True,
            "setpoint_f": 84,
        }
    ]
    slots = slots_from_config(raw)
    when = datetime(2026, 10, 5, 9, 0)
    nxt = next_transition(slots, "heater", when, looking_for=True)
    assert nxt == datetime(2026, 10, 5, 10, 0)
