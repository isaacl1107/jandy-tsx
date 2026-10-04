"""Tests for weekly schedule helpers."""

from datetime import datetime, time

from custom_components.jandy_tcx.schedule import (
    ScheduleSlot,
    desired_states,
    managed_targets,
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


def test_light_schedule_and_empty_defaults():
    from custom_components.jandy_tcx.schedule import DEFAULT_SCHEDULES

    assert DEFAULT_SCHEDULES == []
    slots = [
        ScheduleSlot(
            id="l1",
            target="light",
            days=(5, 6),
            start=time(19, 0),
            end=time(23, 0),
        )
    ]
    saturday_evening = datetime(2026, 10, 10, 20, 0)
    saturday_afternoon = datetime(2026, 10, 10, 15, 0)
    assert desired_states(slots, saturday_evening)["light"]["on"] is True
    assert desired_states(slots, saturday_afternoon)["light"]["on"] is False
    assert desired_states(slots, saturday_evening)["heater"]["on"] is False


def test_water_feature_forces_pump():
    slots = [
        ScheduleSlot(
            id="wf1",
            target="water_feature",
            days=(0,),
            start=time(12, 0),
            end=time(14, 0),
        )
    ]
    noon = datetime(2026, 10, 5, 12, 30)
    assert desired_states(slots, noon)["water_feature"]["on"] is True
    assert desired_states(slots, noon)["pump"]["on"] is True


def test_pump_rpm_and_light_color_in_desired_states():
    slots = [
        ScheduleSlot(
            id="p1",
            target="pump",
            days=(0,),
            start=time(8, 0),
            end=time(12, 0),
            rpm=2500,
        ),
        ScheduleSlot(
            id="l1",
            target="light",
            days=(0,),
            start=time(8, 0),
            end=time(12, 0),
            light_color=3,
        ),
    ]
    noon = datetime(2026, 10, 5, 10, 0)
    desired = desired_states(slots, noon)
    assert desired["pump"]["on"] is True
    assert desired["pump"]["rpm"] == 2500
    assert desired["light"]["on"] is True
    assert desired["light"]["color"] == 3

    raw = [
        {
            "id": "p1",
            "target": "pump",
            "days": [0],
            "start": "08:00",
            "end": "12:00",
            "rpm": 2200,
        }
    ]
    parsed = slots_from_config(raw)
    assert parsed[0].rpm == 2200


def test_managed_targets_only_listed_equipment():
    slots = [
        ScheduleSlot(
            id="h1",
            target="heater",
            days=(0,),
            start=time(10, 0),
            end=time(18, 0),
            setpoint_f=84,
        )
    ]
    assert managed_targets(slots) == {"heater"}
    # Light is not managed — schedules must not force it off.
    assert "light" not in managed_targets(slots)


def test_strip_legacy_default_schedules():
    from custom_components.jandy_tcx.schedule import (
        LEGACY_DEFAULT_SCHEDULE_IDS,
        strip_legacy_default_schedules,
    )

    raw = [
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
        {
            "id": "abc123def0",
            "target": "heater",
            "days": [0, 1, 2, 3, 4],
            "start": "10:00",
            "end": "18:00",
            "enabled": True,
            "setpoint_f": 84,
        },
    ]
    kept, removed = strip_legacy_default_schedules(raw)
    assert removed == 3
    assert LEGACY_DEFAULT_SCHEDULE_IDS == {
        "weekday-heat",
        "weekend-heat",
        "daily-filter",
    }
    assert [item["id"] for item in kept] == ["abc123def0"]
    assert strip_legacy_default_schedules([]) == ([], 0)
    assert strip_legacy_default_schedules(None) == ([], 0)
