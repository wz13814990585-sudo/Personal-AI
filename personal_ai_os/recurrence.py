"""Generate instances only from rules explicitly saved by the user."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .memory import approved_study_avoid_ranges
from .schedule_rules import Interval, validate_schedule
from .storage import Repository, _rows
from uuid import uuid4


def local_start(day: date, clock: time, zone: ZoneInfo) -> tuple[datetime | None, str | None]:
    """Reject wall times in DST gaps or folds; do not silently shift them."""
    naive = datetime.combine(day, clock)
    options: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive:
            options.append(candidate)
    if not options:
        return None, "nonexistent_local_time"
    if len(options) == 2 and options[0].utcoffset() != options[1].utcoffset():
        return None, "ambiguous_local_time"
    return options[0], None


def _intervals(rows: list[dict]) -> list[Interval]:
    return [Interval(datetime.fromisoformat(row["start_at_utc"]),
                     datetime.fromisoformat(row["end_at_utc"])) for row in rows]


def generate_instances(
    repository: Repository, rule_id: str, start_date: date, end_date: date,
    *, as_of_local_date: date | None = None,
) -> list[dict]:
    """Scan current/future local dates. Existing instances remain fixed after edits."""
    if end_date < start_date or (end_date - start_date).days > 31:
        raise ValueError("invalid_generation_window")
    rule = repository.get_recurrence_rule(rule_id)
    zone = ZoneInfo(rule["timezone"])
    today = as_of_local_date or datetime.now(zone).date()
    cursor_day = max(start_date, today, date.fromisoformat(rule["start_date"]))
    last_day = min(end_date, date.fromisoformat(rule["end_date"])) if rule["end_date"] else end_date
    generated: list[dict] = []
    while cursor_day <= last_day:
        day = cursor_day
        cursor_day += timedelta(days=1)
        with repository.transaction() as connection:
            current = connection.execute("SELECT * FROM recurrence_rules WHERE id=?", (rule_id,)).fetchone()
            if current is None:
                raise KeyError(rule_id)
            if current["status"] != "active":
                continue
            if day < date.fromisoformat(current["start_date"]) or (
                current["end_date"] and day > date.fromisoformat(current["end_date"])
            ):
                continue
            zone = ZoneInfo(current["timezone"])
            weekdays = json.loads(current["weekdays_json"])
            if current["frequency"] == "weekly_days" and day.weekday() not in weekdays:
                continue
            if connection.execute(
                "SELECT 1 FROM recurrence_instances WHERE rule_id=? AND local_date=?",
                (rule_id, day.isoformat()),
            ).fetchone():
                continue
            start = None
            end = None
            reason = "no_preferred_time"
            if current["preferred_time"]:
                start, reason = local_start(day, time.fromisoformat(current["preferred_time"]), zone)
                if start is not None:
                    end = (start.astimezone(timezone.utc) + timedelta(
                        minutes=current["estimated_minutes"]
                    )).astimezone(zone)
                    blocks = _rows(connection.execute("SELECT * FROM time_blocks"))
                    tasks = _rows(connection.execute(
                        "SELECT start_at_utc,end_at_utc FROM tasks "
                        "WHERE start_at_utc IS NOT NULL AND status!='completed'"
                    ))
                    avoid = []
                    if current["domain"] == "study":
                        avoid, _ = approved_study_avoid_ranges(_rows(connection.execute(
                            "SELECT * FROM memories WHERE status='approved'"
                        )))
                    issues = validate_schedule(
                        Interval(start, end),
                        availability=_intervals([row for row in blocks if row["kind"] == "available"]),
                        busy=_intervals([row for row in blocks if row["kind"] == "busy"]),
                        tasks=_intervals(tasks),
                        avoid_local_ranges=avoid, timezone_name=current["timezone"],
                    )
                    if issues:
                        start = end = None
                        reason = ",".join(issues)
            task_id = uuid4().hex
            connection.execute(
                "INSERT INTO tasks(id,title,status,priority,estimated_minutes,start_at_utc,"
                "end_at_utc,domain,recurrence_rule_id) VALUES (?,?,'pending',?,?,?,?,?,?)",
                (task_id, current["title"], current["priority"], current["estimated_minutes"],
                 start.astimezone(timezone.utc).isoformat() if start else None,
                 end.astimezone(timezone.utc).isoformat() if end else None,
                 current["domain"], rule_id),
            )
            instance_id = uuid4().hex
            connection.execute(
                "INSERT INTO recurrence_instances(id,rule_id,local_date,task_id,"
                "unassigned_reason,created_at_utc) VALUES (?,?,?,?,?,?)",
                (instance_id, rule_id, day.isoformat(), task_id, reason,
                 datetime.now(timezone.utc).isoformat()),
            )
            generated.append({"id": instance_id, "rule_id": rule_id,
                              "local_date": day.isoformat(), "task_id": task_id,
                              "unassigned_reason": reason})
    return generated
