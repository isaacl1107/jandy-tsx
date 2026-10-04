"""Weekly equipment schedule helpers for Jandy TCX."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Literal

ScheduleTarget = Literal["heater", "pump", "light", "water_feature"]
SCHEDULE_TARGETS: tuple[ScheduleTarget, ...] = (
    "heater",
    "pump",
    "light",
    "water_feature",
)


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
    target = str(data.get("target", "heater"))
    if target not in SCHEDULE_TARGETS:
        target = "heater"
    days = tuple(int(day) for day in data.get("days", list(range(7))))
    return ScheduleSlot(
        id=str(data["id"]),
        target=target,  # type: ignore[arg-type]
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


def managed_targets(slots: list[ScheduleSlot]) -> set[ScheduleTarget]:
    """Targets that have at least one enabled schedule slot."""
    return {slot.target for slot in slots if slot.enabled}


def slots_from_config(raw: list[dict[str, Any]] | None) -> list[ScheduleSlot]:
    if not raw:
        return []
    return [slot_from_dict(item) for item in raw]


def desired_states(
    slots: list[ScheduleSlot], when: datetime
) -> dict[ScheduleTarget, dict[str, Any]]:
    """Return desired heater/pump/light/water-feature state from active slots."""
    result: dict[ScheduleTarget, dict[str, Any]] = {
        "heater": {"on": False, "setpoint_f": None},
        "pump": {"on": False},
        "light": {"on": False},
        "water_feature": {"on": False},
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
        elif slot.target == "light":
            result["light"]["on"] = True
        elif slot.target == "water_feature":
            result["water_feature"]["on"] = True
    # Heater and water features need filtration running.
    if result["heater"]["on"] or result["water_feature"]["on"]:
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
            pump_required = any(
                slot.target in {"heater", "water_feature"} and slot.active_at(probe)
                for slot in slots
            )
            active = active or pump_required
        if active == looking_for:
            if minutes == 0:
                continue
            previous = cursor + timedelta(minutes=minutes - 1)
            prev_active = any(
                slot.target == target and slot.active_at(previous)
                for slot in slots
            )
            if target == "pump":
                prev_required = any(
                    slot.target in {"heater", "water_feature"}
                    and slot.active_at(previous)
                    for slot in slots
                )
                prev_active = prev_active or prev_required
            if prev_active != looking_for:
                return probe
    return None


# New installs start with no schedules; users add them in Configure.
DEFAULT_SCHEDULES: list[dict[str, Any]] = []

# Sample slots seeded by early releases. User-created IDs are uuid hex, so
# these three names only ever belonged to the old stock defaults.
LEGACY_DEFAULT_SCHEDULE_IDS = frozenset(
    {"weekday-heat", "weekend-heat", "daily-filter"}
)


def is_legacy_default_schedule(item: dict[str, Any]) -> bool:
    """True for the stock sample schedules from early releases."""
    return str(item.get("id", "")) in LEGACY_DEFAULT_SCHEDULE_IDS


def strip_legacy_default_schedules(
    raw: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Drop stock sample schedules. Returns (kept, removed_count)."""
    if not raw:
        return [], 0
    kept: list[dict[str, Any]] = []
    removed = 0
    for item in raw:
        if is_legacy_default_schedule(item):
            removed += 1
            continue
        kept.append(dict(item))
    return kept, removed
