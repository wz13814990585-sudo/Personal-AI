from datetime import datetime, time, timezone

import pytest

from personal_ai_os.contracts import TaskCreate, TimeBlockCreate
from personal_ai_os.schedule_rules import Interval, overlaps, validate_schedule
from personal_ai_os.storage import Repository


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 9, hour, minute, tzinfo=timezone.utc)


def test_half_open_overlap_and_schedule_rules():
    slot = Interval(dt(10), dt(11))
    assert not overlaps(slot, Interval(dt(11), dt(12)))
    assert overlaps(slot, Interval(dt(10, 30), dt(11, 30)))
    assert validate_schedule(slot, availability=[], busy=[], tasks=[]) == ["no_availability"]
    assert validate_schedule(
        slot, availability=[Interval(dt(9), dt(12))], busy=[], tasks=[]
    ) == []
    assert "busy_overlap" in validate_schedule(
        slot, availability=[Interval(dt(9), dt(12))],
        busy=[Interval(dt(10, 30), dt(11, 30))], tasks=[],
    )
    assert "after_deadline" in validate_schedule(
        slot, availability=[Interval(dt(9), dt(12))],
        busy=[], tasks=[], due_at=dt(10, 30),
    )


def test_local_morning_avoidance_and_timezone_validation():
    morning_sydney = Interval(dt(22), dt(23))  # 09:00-10:00 the next day in Sydney
    issues = validate_schedule(
        morning_sydney,
        availability=[Interval(dt(21), dt(23))], busy=[], tasks=[],
        avoid_local_ranges=[(time(0), time(12))],
    )
    assert "avoided_local_time" in issues
    with pytest.raises(ValueError, match="timezone-aware"):
        Interval(datetime(2026, 10, 9, 10), dt(11))


def test_repository_rejects_conflicting_scheduled_writes(tmp_path):
    repository = Repository(tmp_path / "db.sqlite3")
    repository.initialize()
    repository.create_time_block(TimeBlockCreate(kind="available", start_at=dt(9), end_at=dt(18)))
    first = repository.create_task(TaskCreate(title="First", start_at=dt(10), end_at=dt(11)))
    with pytest.raises(ValueError, match="task_overlap"):
        repository.create_task(TaskCreate(title="Overlap", start_at=dt(10, 30), end_at=dt(11, 30)))
    with pytest.raises(ValueError, match="busy_overlap_with_task"):
        repository.create_time_block(
            TimeBlockCreate(kind="busy", start_at=dt(10, 30), end_at=dt(11, 30))
        )
    with pytest.raises(ValueError, match="outside_availability"):
        repository.create_task(TaskCreate(title="Outside", start_at=dt(18), end_at=dt(19)))
    availability = repository.list_time_blocks("available")[0]
    with pytest.raises(ValueError, match="scheduled_task_outside_availability"):
        repository.update_time_block(
            availability["id"],
            TimeBlockCreate(kind="available", start_at=dt(12), end_at=dt(18)),
        )
    with pytest.raises(ValueError, match="scheduled_task_outside_availability"):
        repository.delete_time_block(availability["id"])
    assert [row["id"] for row in repository.list_tasks()] == [first["id"]]
    assert repository.get_time_block(availability["id"])["start_at_utc"] == dt(9).isoformat()
