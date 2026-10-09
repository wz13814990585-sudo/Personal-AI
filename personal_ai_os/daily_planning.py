"""Deterministic daily suggestions and user-approved atomic task changes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .contracts import DailyPlanAction, DailyPlanDraft
from .memory import approved_study_avoid_ranges
from .schedule_rules import Interval, validate_schedule
from .storage import Repository, _rows, _timestamp, _now


def _fingerprint(connection: sqlite3.Connection) -> str:
    state = {}
    for table in ("tasks", "time_blocks", "recurrence_rules", "recurrence_instances", "memories", "settings"):
        condition = " WHERE status='approved'" if table == "memories" else ""
        state[table] = _rows(connection.execute(f"SELECT * FROM {table}{condition} ORDER BY 1"))
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _source(task: dict[str, Any]) -> str:
    return (f"recurrence:{task['recurrence_rule_id']}" if task["recurrence_rule_id"]
            else f"task:{task['id']}")


def _interval(row: dict[str, Any]) -> Interval:
    return Interval(datetime.fromisoformat(row["start_at_utc"]),
                    datetime.fromisoformat(row["end_at_utc"]))


def _row_to_draft(row: sqlite3.Row) -> DailyPlanDraft:
    return DailyPlanDraft(
        id=row["id"], local_date=date.fromisoformat(row["local_date"]),
        timezone=row["timezone"], revision=row["revision"],
        baseline_fingerprint=row["baseline_fingerprint"], status=row["status"],
        actions=json.loads(row["actions_json"]), conflicts=json.loads(row["conflicts_json"]),
    )


def get_daily_plan(repository: Repository, plan_id: str) -> DailyPlanDraft:
    with repository.connection() as connection:
        row = connection.execute("SELECT * FROM daily_plan_proposals WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise KeyError(plan_id)
    return _row_to_draft(row)


def list_daily_plans(repository: Repository) -> list[DailyPlanDraft]:
    with repository.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM daily_plan_proposals ORDER BY created_at_utc DESC,rowid DESC"
        ).fetchall()
    return [_row_to_draft(row) for row in rows]


def _plan_actions(
    connection: sqlite3.Connection, local_date: date, timezone_name: str,
    *, as_of: datetime,
) -> tuple[list[DailyPlanAction], list[str]]:
    zone = ZoneInfo(timezone_name)
    day_start = datetime.combine(local_date, time.min, zone)
    day_end = datetime.combine(local_date + timedelta(days=1), time.min, zone)
    day_start_utc = day_start.astimezone(timezone.utc)
    day_end_utc = day_end.astimezone(timezone.utc)
    if day_start_utc >= day_end_utc:
        raise ValueError("invalid_local_date")
    tasks = _rows(connection.execute("SELECT * FROM tasks ORDER BY rowid"))
    blocks = _rows(connection.execute("SELECT * FROM time_blocks ORDER BY start_at_utc"))
    all_available = [_interval(row) for row in blocks if row["kind"] == "available"]
    available = [window for window in all_available
                 if window.start.astimezone(timezone.utc) < day_end_utc
                 and window.end.astimezone(timezone.utc) > day_start_utc]
    busy = [_interval(row) for row in blocks if row["kind"] == "busy"]
    avoid, _ = approved_study_avoid_ranges(_rows(connection.execute(
        "SELECT * FROM memories WHERE status='approved'"
    )))
    instance_dates = {row["task_id"]: row["local_date"] for row in connection.execute(
        "SELECT task_id,local_date FROM recurrence_instances WHERE task_id IS NOT NULL"
    )}
    unscheduled = [row for row in tasks
                   if row["status"] != "completed" and row["start_at_utc"] is None
                   and (row["id"] not in instance_dates
                        or instance_dates[row["id"]] <= local_date.isoformat())]
    as_of_utc = as_of.astimezone(timezone.utc)
    scheduled_to_move: list[tuple[dict[str, Any], str]] = []
    for row in tasks:
        if row["status"] == "completed" or not row["start_at_utc"]:
            continue
        old = _interval(row)
        overdue = (old.end.astimezone(timezone.utc) <= as_of_utc
                   and local_date >= as_of.astimezone(zone).date())
        overlaps_day = (old.start.astimezone(timezone.utc) < day_end_utc
                        and old.end.astimezone(timezone.utc) > day_start_utc)
        if not (overdue or overlaps_day):
            continue
        if overdue:
            scheduled_to_move.append((row, "逾期未完成，建议重新安排"))
            continue
        old_issues = validate_schedule(
            old, availability=all_available, busy=busy, tasks=[],
            due_at=datetime.fromisoformat(row["due_at_utc"]) if row["due_at_utc"] else None,
            avoid_local_ranges=avoid if row["domain"] == "study" else [],
            timezone_name=timezone_name,
        )
        if old_issues:
            scheduled_to_move.append((row, "原安排与当前时间条件冲突：" + ", ".join(old_issues)))
    moving_ids = {row["id"] for row, _ in scheduled_to_move}
    occupied = [_interval(row) for row in tasks
                if row["start_at_utc"] and row["status"] != "completed"
                and row["id"] not in moving_ids]
    rank = {"high": 0, "medium": 1, "low": 2}
    unscheduled.sort(key=lambda row: (rank[row["priority"]], row["due_at_utc"] or "9999", row["id"]))
    actions: list[DailyPlanAction] = []
    conflicts: list[str] = []
    for row, reason_text in [*scheduled_to_move,
                             *((item, "安排尚未排程的任务") for item in unscheduled)]:
        earliest = day_start_utc
        if local_date == as_of.astimezone(zone).date():
            earliest = max(earliest, as_of.astimezone(timezone.utc))
        chosen = None
        issues_seen: set[str] = set()
        for window in available:
            current = max(window.start.astimezone(timezone.utc), earliest)
            # Fifteen-minute granularity keeps the search bounded and explainable.
            rounded = (current.minute + 14) // 15 * 15
            current = current.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=rounded)
            while current < day_end_utc:
                end = current + timedelta(minutes=row["estimated_minutes"])
                if end > day_end_utc or end > window.end.astimezone(timezone.utc):
                    break
                candidate = Interval(current, end)
                issues = validate_schedule(
                    candidate, availability=available, busy=busy, tasks=occupied,
                    due_at=datetime.fromisoformat(row["due_at_utc"]) if row["due_at_utc"] else None,
                    avoid_local_ranges=avoid if row["domain"] == "study" else [],
                    timezone_name=timezone_name,
                )
                if not issues:
                    chosen = candidate
                    break
                issues_seen.update(issues)
                current += timedelta(minutes=15)
            if chosen:
                break
        reason = None if chosen else (
            "no_availability" if not available else
            "no_legal_time:" + (",".join(sorted(issues_seen)) if issues_seen else "insufficient_window")
        )
        if chosen:
            occupied.append(chosen)
        else:
            conflicts.append(f"{row['title']}: {reason}")
        actions.append(DailyPlanAction(
            action_id=uuid4().hex, kind="reschedule_task", task_id=row["id"],
            title=row["title"], domain=row["domain"], priority=row["priority"],
            estimated_minutes=row["estimated_minutes"],
            due_at=datetime.fromisoformat(row["due_at_utc"]) if row["due_at_utc"] else None,
            old_start_at=datetime.fromisoformat(row["start_at_utc"]) if row["start_at_utc"] else None,
            old_end_at=datetime.fromisoformat(row["end_at_utc"]) if row["end_at_utc"] else None,
            old_version=row["version"],
            new_start_at=chosen.start if chosen else None,
            new_end_at=chosen.end if chosen else None,
            source=_source(row), reason=reason_text,
            unassigned_reason=reason,
        ))
    return actions, conflicts


def propose_daily_plan(
    repository: Repository, local_date: date, timezone_name: str,
    *, trigger_key: str | None = None, as_of: datetime | None = None,
) -> DailyPlanDraft:
    zone = ZoneInfo(timezone_name)
    now = as_of or datetime.now(zone)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of_requires_timezone")
    key = trigger_key or uuid4().hex
    with repository.transaction() as connection:
        existing = connection.execute(
            "SELECT * FROM daily_plan_proposals WHERE trigger_key=?", (key,)
        ).fetchone()
        if existing:
            return _row_to_draft(existing)
        fingerprint = _fingerprint(connection)
        actions, conflicts = _plan_actions(connection, local_date, timezone_name, as_of=now)
        plan_id = uuid4().hex
        connection.execute(
            "INSERT INTO daily_plan_proposals(id,local_date,timezone,trigger_key,"
            "baseline_fingerprint,actions_json,conflicts_json,created_at_utc,updated_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (plan_id, local_date.isoformat(), timezone_name, key, fingerprint,
             json.dumps([item.model_dump(mode="json") for item in actions], ensure_ascii=False),
             json.dumps(conflicts, ensure_ascii=False), _now(), _now()),
        )
        row = connection.execute("SELECT * FROM daily_plan_proposals WHERE id=?", (plan_id,)).fetchone()
        return _row_to_draft(row)


def _validate_actions(
    connection: sqlite3.Connection, draft: DailyPlanDraft,
    actions: list[DailyPlanAction],
) -> list[str]:
    ids = [item.action_id for item in actions]
    task_ids = [item.task_id for item in actions if item.task_id]
    if len(ids) != len(set(ids)) or len(task_ids) != len(set(task_ids)):
        raise ValueError("duplicate_daily_action")
    tasks = {row["id"]: row for row in _rows(connection.execute("SELECT * FROM tasks"))}
    moving = {item.task_id for item in actions
              if item.kind == "reschedule_task" and item.new_start_at is not None}
    blocks = _rows(connection.execute("SELECT * FROM time_blocks"))
    available = [_interval(row) for row in blocks if row["kind"] == "available"]
    busy = [_interval(row) for row in blocks if row["kind"] == "busy"]
    occupied = [_interval(row) for row in tasks.values()
                if row["start_at_utc"] and row["status"] != "completed" and row["id"] not in moving]
    avoid, _ = approved_study_avoid_ranges(_rows(connection.execute(
        "SELECT * FROM memories WHERE status='approved'"
    )))
    issues: list[str] = []
    for item in actions:
        if item.kind == "create_task":
            if item.source != "user_daily_plan":
                raise PermissionError("daily_create_requires_user_source")
        else:
            task = tasks.get(item.task_id)
            if task is None or task["status"] == "completed":
                raise ValueError("daily_task_unavailable")
            if (item.title != task["title"] or item.domain != task["domain"]
                or item.priority != task["priority"] or item.estimated_minutes != task["estimated_minutes"]
                or _timestamp(item.due_at) != task["due_at_utc"]
                or _timestamp(item.old_start_at) != task["start_at_utc"]
                or _timestamp(item.old_end_at) != task["end_at_utc"]
                or item.old_version != task["version"] or item.source != _source(task)):
                raise ValueError("daily_task_source_changed")
        if item.new_start_at is None:
            issues.append(f"{item.action_id}: {item.unassigned_reason or 'unassigned'}")
            continue
        if item.new_start_at.astimezone(ZoneInfo(draft.timezone)).date() != draft.local_date:
            issues.append(f"{item.action_id}: wrong_local_date")
            continue
        if item.new_end_at.astimezone(timezone.utc) > datetime.combine(
            draft.local_date + timedelta(days=1), time.min, ZoneInfo(draft.timezone)
        ).astimezone(timezone.utc):
            issues.append(f"{item.action_id}: crosses_local_day")
            continue
        candidate = Interval(item.new_start_at, item.new_end_at)
        found = validate_schedule(
            candidate, availability=available, busy=busy, tasks=occupied,
            due_at=item.due_at, avoid_local_ranges=avoid if item.domain == "study" else [],
            timezone_name=draft.timezone,
        )
        if (candidate.end.astimezone(timezone.utc) - candidate.start.astimezone(timezone.utc)).total_seconds() < item.estimated_minutes * 60:
            found.append("insufficient_duration")
        issues.extend(f"{item.action_id}: {issue}" for issue in found)
        if not found:
            occupied.append(candidate)
    return issues


def edit_daily_plan(
    repository: Repository, plan_id: str, revision: int,
    actions: list[DailyPlanAction | dict[str, Any]],
) -> DailyPlanDraft:
    checked = [DailyPlanAction.model_validate(
        item.model_dump() if isinstance(item, DailyPlanAction) else item
    ) for item in actions]
    with repository.transaction() as connection:
        row = connection.execute("SELECT * FROM daily_plan_proposals WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(plan_id)
        draft = _row_to_draft(row)
        if draft.status != "draft" or draft.revision != revision:
            raise ValueError("stale_daily_revision")
        if _fingerprint(connection) != draft.baseline_fingerprint:
            raise ValueError("stale_plan")
        conflicts = _validate_actions(connection, draft, checked)
        connection.execute(
            "UPDATE daily_plan_proposals SET revision=revision+1,actions_json=?,conflicts_json=?,"
            "updated_at_utc=? WHERE id=?",
            (json.dumps([item.model_dump(mode="json") for item in checked], ensure_ascii=False),
             json.dumps(conflicts, ensure_ascii=False), _now(), plan_id),
        )
        return _row_to_draft(connection.execute(
            "SELECT * FROM daily_plan_proposals WHERE id=?", (plan_id,)
        ).fetchone())


def _apply_action(connection: sqlite3.Connection, item: DailyPlanAction) -> dict[str, Any]:
    if item.kind == "create_task":
        task_id = uuid4().hex
        connection.execute(
            "INSERT INTO tasks(id,title,status,priority,estimated_minutes,due_at_utc,"
            "start_at_utc,end_at_utc,domain) VALUES (?,?,'pending',?,?,?,?,?,?)",
            (task_id, item.title, item.priority, item.estimated_minutes,
             _timestamp(item.due_at), _timestamp(item.new_start_at),
             _timestamp(item.new_end_at), item.domain),
        )
    else:
        task_id = item.task_id
        changed = connection.execute(
            "UPDATE tasks SET start_at_utc=?,end_at_utc=?,version=version+1 "
            "WHERE id=? AND version=? AND status!='completed'",
            (_timestamp(item.new_start_at), _timestamp(item.new_end_at),
             task_id, item.old_version),
        ).rowcount
        if changed != 1:
            raise ValueError("stale_daily_task")
    return dict(connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def approve_daily_plan(repository: Repository, plan_id: str, revision: int) -> list[dict[str, Any]]:
    with repository.transaction() as connection:
        row = connection.execute("SELECT * FROM daily_plan_proposals WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(plan_id)
        draft = _row_to_draft(row)
        if draft.revision != revision:
            raise ValueError("stale_daily_revision")
        if draft.status == "committed":
            return json.loads(row["result_json"])
        if draft.status != "draft":
            raise ValueError("daily_plan_not_draft")
        if _fingerprint(connection) != draft.baseline_fingerprint:
            raise ValueError("stale_plan")
        if not draft.actions:
            raise ValueError("empty_daily_plan")
        conflicts = _validate_actions(connection, draft, draft.actions)
        if conflicts:
            raise ValueError("daily_plan_conflict:" + ";".join(conflicts))
        result = [_apply_action(connection, item) for item in draft.actions]
        connection.execute(
            "UPDATE daily_plan_proposals SET status='committed',result_json=?,updated_at_utc=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), _now(), plan_id),
        )
        connection.execute(
            "INSERT INTO trace_events(id,actor,event_type,summary_json,created_at_utc) "
            "VALUES (?,'user','daily_plan_committed',?,?)",
            (uuid4().hex, json.dumps({"plan_id": plan_id, "revision": revision,
                                      "actions": len(result)}), _now()),
        )
        return result


def daily_overview(
    repository: Repository, local_date: date, timezone_name: str,
    *, as_of: datetime | None = None,
) -> dict[str, Any]:
    zone = ZoneInfo(timezone_name)
    now = as_of or datetime.now(zone)
    reference = (now if local_date == now.astimezone(zone).date()
                 else datetime.combine(local_date, time(12), zone))
    day_start_utc = datetime.combine(local_date, time.min, zone).astimezone(timezone.utc)
    day_end_utc = datetime.combine(local_date + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    tasks = repository.list_tasks()
    instances = {item["task_id"]: item for item in repository.list_recurrence_instances()}
    visible = []
    for task in tasks:
        scheduled_today = bool(
            task["start_at_utc"]
            and datetime.fromisoformat(task["start_at_utc"]).astimezone(timezone.utc) < day_end_utc
            and datetime.fromisoformat(task["end_at_utc"]).astimezone(timezone.utc) > day_start_utc
        )
        instance = instances.get(task["id"])
        if scheduled_today or (task["status"] != "completed" and task["start_at_utc"] is None
                                   and (not instance or instance["local_date"] <= local_date.isoformat())):
            visible.append({**task, "recurrence_local_date": instance["local_date"] if instance else None,
                            "unassigned_reason": instance["unassigned_reason"] if instance else
                            ("awaiting_schedule" if task["start_at_utc"] is None else None)})
    blocks = []
    for row in repository.list_time_blocks():
        interval = _interval(row)
        if (interval.start.astimezone(timezone.utc) < day_end_utc
            and interval.end.astimezone(timezone.utc) > day_start_utc):
            blocks.append(row)
    habits = []
    for habit in repository.list_habits():
        checks = repository.list_habit_checkins(habit["id"])
        habit_date = reference.astimezone(ZoneInfo(habit["timezone"])).date()
        week_start = habit_date - timedelta(days=habit_date.weekday())
        done = sum(row["completed"] for row in checks
                   if week_start.isoformat() <= row["local_date"] <= habit_date.isoformat())
        habits.append({**habit, "completed_this_week": done,
                       "habit_local_date": habit_date.isoformat(),
                       "checked_today": next((bool(row["completed"]) for row in checks
                                              if row["local_date"] == habit_date.isoformat()), None)})
    return {"local_date": local_date.isoformat(), "timezone": timezone_name,
            "tasks": visible, "time_blocks": blocks, "habits": habits}
