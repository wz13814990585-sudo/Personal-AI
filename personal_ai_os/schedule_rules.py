"""Deterministic time validation; model suggestions never bypass these rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Intervals require timezone-aware datetimes")
        if self.start >= self.end:
            raise ValueError("Interval start must be before end")


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime must include a timezone")
    return value.astimezone(timezone.utc)


def overlaps(left: Interval, right: Interval) -> bool:
    return to_utc(left.start) < to_utc(right.end) and to_utc(right.start) < to_utc(left.end)


def validate_schedule(
    candidate: Interval,
    *,
    availability: list[Interval],
    busy: list[Interval],
    tasks: list[Interval],
    due_at: datetime | None = None,
    avoid_local_ranges: list[tuple[time, time]] | None = None,
    timezone_name: str = "Australia/Sydney",
) -> list[str]:
    """Return stable issue codes; an empty list means the slot is valid."""
    issues: list[str] = []
    if not availability:
        issues.append("no_availability")
    elif not any(
        to_utc(window.start) <= to_utc(candidate.start)
        and to_utc(candidate.end) <= to_utc(window.end)
        for window in availability
    ):
        issues.append("outside_availability")

    if any(overlaps(candidate, block) for block in busy):
        issues.append("busy_overlap")
    if any(overlaps(candidate, task) for task in tasks):
        issues.append("task_overlap")
    if due_at is not None and to_utc(candidate.end) > to_utc(due_at):
        issues.append("after_deadline")

    zone = ZoneInfo(timezone_name)
    local_start = candidate.start.astimezone(zone)
    local_end = candidate.end.astimezone(zone)
    for start_time, end_time in avoid_local_ranges or []:
        if start_time >= end_time:
            raise ValueError("Avoidance time ranges must be within one local day")
        day = local_start.date()
        while day <= local_end.date():
            forbidden = Interval(
                datetime.combine(day, start_time, zone),
                datetime.combine(day, end_time, zone),
            )
            if overlaps(candidate, forbidden):
                issues.append("avoided_local_time")
                break
            day += timedelta(days=1)
    return issues
