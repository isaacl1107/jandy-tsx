"""Weekly heater / pump schedule helpers for Jandy TCX."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Literal

ScheduleTarget = Literal["heater", "pump"]


@dataclass(frozen=True)
class ScheduleSlot:
    """One recurring on/off window."""

    id: str
    target: ScheduleTarget
    days: tuple[int, ...]  # Monday=0 ... Sunday=6
    start: time
    end: time
    enabled: bool = True
    setpoint_f: float | None = None

    def active_at(self, when: datetime) -> bool:
        if not self.enabled or when.weekday() not in self.days:
            return False
        current = when.time().replace(second=0, microsecond=0)
        if self.start <= self.end:
            return self.start <= current < self.end
        # Overnight window, e.g. 22:00 → 06:00
        return current >= self.start or current < self.end


def parse_hhmm(value: str) -> time:
    hour_s, minute_s = value.split(":", 1)
    return time(hour=int(hour_s), minute=int(minute_s))


def slot_from_dict(data: dict[str, Any]) -> ScheduleSlot:
    days = tuple(int(day) for day in data.get("days", list(range(7))))
    return ScheduleSlot(
        id=str(data["id"]),
        target=data.get("target", "heater"),
        days=days,
        start=parse_hhmm(str(data["start"])),
        end=parse_hhmm(str(data["end"])),
        enabled=bool(data.get("enabled", True)),
        setpoint_f=(
            float(data["setpoint_f"])
            if data.get("setpoint_f") is not None
            else None
        ),
    )


def slots_from_config(raw: list[dict[str, Any]] | None) -> list[ScheduleSlot]:
    if not raw:
        return []
    return [slot_from_dict(item) for item in raw]


def desired_states(
    slots: list[ScheduleSlot], when: datetime
) -> dict[ScheduleTarget, dict[str, Any]]:
    """Return desired heater/pump state from all active slots."""
    result: dict[ScheduleTarget, dict[str, Any]] = {
        "heater": {"on": False, "setpoint_f": None},
        "pump": {"on": False},
    }
    for slot in slots:
        if not slot.active_at(when):
            continue
        if slot.target == "heater":
            result["heater"]["on"] = True
            if slot.setpoint_f is not None:
                result["heater"]["setpoint_f"] = slot.setpoint_f
        elif slot.target == "pump":
            result["pump"]["on"] = True
    # Heater implies filtration for safety.
    if result["heater"]["on"]:
        result["pump"]["on"] = True
    return result


def next_transition(
    slots: list[ScheduleSlot],
    target: ScheduleTarget,
    when: datetime,
    *,
    looking_for: bool,
    horizon_hours: int = 24 * 8,
) -> datetime | None:
    """Find the next datetime when target should become looking_for."""
    cursor = when.replace(second=0, microsecond=0)
    for minutes in range(0, horizon_hours * 60):
        probe = cursor + timedelta(minutes=minutes)
        active = any(
            slot.target == target and slot.active_at(probe) for slot in slots
        )
        # heater-implied pump is handled by callers via desired_states
        if target == "pump":
            heater_on = any(
                slot.target == "heater" and slot.active_at(probe)
                for slot in slots
            )
            active = active or heater_on
        if active == looking_for:
            if minutes == 0:
                continue
            previous = cursor + timedelta(minutes=minutes - 1)
            prev_active = any(
                slot.target == target and slot.active_at(previous)
                for slot in slots
            )
            if target == "pump":
                prev_heater = any(
                    slot.target == "heater" and slot.active_at(previous)
                    for slot in slots
                )
                prev_active = prev_active or prev_heater
            if prev_active != looking_for:
                return probe
    return None


DEFAULT_SCHEDULES: list[dict[str, Any]] = [
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
