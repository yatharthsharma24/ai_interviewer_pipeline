"""Slot allocation for interview rounds.

Deliberately pure: given a round's window and a count, produce the slots. No database, no
clock, no I/O — so the awkward parts (day rollover, weekends, DST) are directly testable.

Times are computed in the round's local timezone, then converted to UTC for storage. That
ordering matters: "10:00 every day" is a wall-clock promise to the candidate, and computing
it in local time keeps it true across a DST change instead of drifting by an hour.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SchedulingError(ValueError):
    pass


@dataclass(frozen=True)
class SlotWindow:
    """The scheduling parameters of a round, independent of the ORM."""

    start_date: dt.date
    day_start_time: dt.time
    day_end_time: dt.time
    slot_minutes: int
    break_minutes: int = 0
    skip_weekends: bool = True
    timezone: str = "Asia/Kolkata"
    max_days: int = 90

    def validate(self) -> None:
        if self.slot_minutes <= 0:
            raise SchedulingError("slot_minutes must be greater than zero.")
        if self.break_minutes < 0:
            raise SchedulingError("break_minutes cannot be negative.")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise SchedulingError(f"Unknown timezone {self.timezone!r}.") from exc
        if self.day_end_time <= self.day_start_time:
            raise SchedulingError(
                f"day_end_time ({self.day_end_time}) must be after "
                f"day_start_time ({self.day_start_time})."
            )

        day_minutes = (
            dt.datetime.combine(dt.date.min, self.day_end_time)
            - dt.datetime.combine(dt.date.min, self.day_start_time)
        ).total_seconds() / 60
        if day_minutes < self.slot_minutes:
            raise SchedulingError(
                f"A {self.slot_minutes}-minute slot does not fit between "
                f"{self.day_start_time} and {self.day_end_time}."
            )

    def slots_per_day(self) -> int:
        """How many slots fit in one day. A slot must *start* before ``day_end_time``."""
        self.validate()
        day_minutes = (
            dt.datetime.combine(dt.date.min, self.day_end_time)
            - dt.datetime.combine(dt.date.min, self.day_start_time)
        ).total_seconds() / 60
        stride = self.slot_minutes + self.break_minutes
        return int((day_minutes - self.slot_minutes) // stride) + 1


def allocate_slots(window: SlotWindow, count: int) -> list[tuple[dt.datetime, dt.datetime]]:
    """Return ``count`` consecutive (start, end) pairs as timezone-aware UTC datetimes.

    Slots fill a day from ``day_start_time``, then roll to the next eligible day.
    """
    window.validate()
    if count < 0:
        raise SchedulingError("count cannot be negative.")
    if count == 0:
        return []

    tz = ZoneInfo(window.timezone)
    per_day = window.slots_per_day()
    stride = dt.timedelta(minutes=window.slot_minutes + window.break_minutes)
    duration = dt.timedelta(minutes=window.slot_minutes)

    slots: list[tuple[dt.datetime, dt.datetime]] = []
    day = window.start_date
    days_examined = 0

    while len(slots) < count:
        if days_examined > window.max_days:
            raise SchedulingError(
                f"Could not place {count} slots within {window.max_days} days of "
                f"{window.start_date}. Widen the daily window or shorten the slots."
            )
        days_examined += 1

        if window.skip_weekends and day.weekday() >= 5:
            day += dt.timedelta(days=1)
            continue

        for index in range(per_day):
            if len(slots) >= count:
                break
            local_start = dt.datetime.combine(day, window.day_start_time, tzinfo=tz) + index * stride
            local_end = local_start + duration
            slots.append(
                (local_start.astimezone(dt.timezone.utc), local_end.astimezone(dt.timezone.utc))
            )

        day += dt.timedelta(days=1)

    return slots


def describe_window(window: SlotWindow, count: int) -> str:
    """One-line human summary, for confirmation prompts before anything is sent."""
    if count == 0:
        return "No candidates to schedule."
    slots = allocate_slots(window, count)
    tz = ZoneInfo(window.timezone)
    first = slots[0][0].astimezone(tz)
    last = slots[-1][0].astimezone(tz)
    days = len({start.astimezone(tz).date() for start, _ in slots})
    return (
        f"{count} slot(s) of {window.slot_minutes} min across {days} day(s): "
        f"{first:%a %d %b %Y %H:%M} to {last:%a %d %b %Y %H:%M} ({window.timezone}), "
        f"{window.slots_per_day()} per day"
    )
