"""SQLite is the authoritative store for application data and execution traces."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .contracts import (
    CustomAgentCreate, GoalCreate, HabitCheckin, HabitCreate, MemoryProposal, PlanDraft,
    RecurrenceRuleCreate, TaskCreate, TaskDraft, TimeBlockCreate,
)
from .memory import approved_study_avoid_ranges, validate_memory_value
from .migrations import migrate
from .recurrence_suggestions import suggestion_rule_id, weekly_three_suggestions
from .schedule_rules import Interval, overlaps, to_utc, validate_schedule


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
    due_at_utc TEXT, status TEXT NOT NULL DEFAULT 'active',
    CHECK (status IN ('active', 'completed', 'archived'))
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, conversation_id TEXT,
    request_text TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL, draft_json TEXT, revision INTEGER NOT NULL DEFAULT 0,
    config_snapshot_json TEXT NOT NULL DEFAULT '{}',
    started_at_utc TEXT, ended_at_utc TEXT,
    CHECK (kind IN ('planning', 'feedback')),
    CHECK (status IN ('pending', 'running', 'waiting_approval', 'success', 'failed', 'cancelled'))
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, goal_id TEXT REFERENCES goals(id) ON DELETE SET NULL,
    title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'medium', estimated_minutes INTEGER NOT NULL,
    due_at_utc TEXT, start_at_utc TEXT, end_at_utc TEXT,
    source_run_id TEXT REFERENCES runs(id), draft_item_id TEXT,
    CHECK (status IN ('pending', 'in_progress', 'completed')),
    CHECK (priority IN ('low', 'medium', 'high')),
    CHECK (estimated_minutes > 0),
    CHECK ((start_at_utc IS NULL) = (end_at_utc IS NULL)),
    UNIQUE (source_run_id, draft_item_id)
);
CREATE TABLE IF NOT EXISTS time_blocks (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, start_at_utc TEXT NOT NULL,
    end_at_utc TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
    CHECK (kind IN ('available', 'busy')),
    CHECK (start_at_utc < end_at_utc)
);
CREATE TABLE IF NOT EXISTS agent_configs (
    role TEXT PRIMARY KEY, description TEXT NOT NULL, instructions TEXT NOT NULL,
    tool_subset_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    CHECK (role IN ('orchestrator', 'memory', 'learning', 'schedule', 'task'))
);
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id TEXT REFERENCES runs(id), body TEXT NOT NULL, created_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_proposals (
    id TEXT PRIMARY KEY, feedback_id TEXT NOT NULL REFERENCES feedback(id),
    kind TEXT NOT NULL, value_json TEXT NOT NULL, source_excerpt TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    CHECK (status IN ('pending', 'approved', 'rejected'))
);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, source_feedback_id TEXT REFERENCES feedback(id),
    kind TEXT NOT NULL, value_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'approved',
    created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
    CHECK (status IN ('approved', 'deleted'))
);
CREATE TABLE IF NOT EXISTS run_steps (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
    step_id TEXT NOT NULL, agent TEXT NOT NULL, depends_on_json TEXT NOT NULL,
    status TEXT NOT NULL, result_json TEXT, error_code TEXT,
    UNIQUE (run_id, step_id),
    CHECK (status IN ('pending', 'running', 'success', 'failed', 'skipped'))
);
CREATE TABLE IF NOT EXISTS trace_events (
    id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), step_id TEXT,
    actor TEXT NOT NULL, event_type TEXT NOT NULL, summary_json TEXT NOT NULL,
    duration_ms INTEGER, created_at_utc TEXT NOT NULL,
    CHECK (actor IN ('user', 'agent', 'system'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_start ON tasks(start_at_utc);
CREATE INDEX IF NOT EXISTS idx_blocks_start ON time_blocks(start_at_utc);
CREATE INDEX IF NOT EXISTS idx_memories_status_kind ON memories(status, kind);
CREATE INDEX IF NOT EXISTS idx_steps_run ON run_steps(run_id);
CREATE INDEX IF NOT EXISTS idx_trace_run_time ON trace_events(run_id, created_at_utc);
"""


def _timestamp(value: datetime | None) -> str | None:
    return to_utc(value).isoformat() if value is not None else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


class Repository:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone() is not None
            if not existing:
                connection.executescript(SCHEMA)
            migrate(connection, self.path, existing_user_db=existing)
            connection.execute("PRAGMA journal_mode=WAL")

    def set_setting(self, key: str, value: Any) -> None:
        if not key:
            raise ValueError("Setting key cannot be empty")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO settings(key, value_json) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row["value_json"]) if row else default

    def list_settings(self) -> dict[str, Any]:
        with self.connection() as connection:
            rows = _rows(connection.execute("SELECT key, value_json FROM settings"))
        return {row["key"]: json.loads(row["value_json"]) for row in rows}

    def create_goal(self, data: GoalCreate) -> dict[str, Any]:
        goal_id = uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO goals(id,title,description,due_at_utc,status) VALUES (?,?,?,?, 'active')",
                (goal_id, data.title, data.description, _timestamp(data.due_at)),
            )
        return self.get_goal(goal_id)

    def get_goal(self, goal_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if row is None:
            raise KeyError(goal_id)
        return dict(row)

    def list_goals(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return _rows(connection.execute("SELECT * FROM goals ORDER BY rowid"))

    def update_goal(self, goal_id: str, data: GoalCreate, status: str = "active") -> dict[str, Any]:
        if status not in {"active", "completed", "archived"}:
            raise ValueError("Invalid goal status")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE goals SET title=?,description=?,due_at_utc=?,status=? WHERE id=?",
                (data.title, data.description, _timestamp(data.due_at), status, goal_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(goal_id)
        return self.get_goal(goal_id)

    def delete_goal(self, goal_id: str) -> None:
        with self.transaction() as connection:
            if connection.execute("DELETE FROM goals WHERE id=?", (goal_id,)).rowcount == 0:
                raise KeyError(goal_id)

    def create_time_block(self, data: TimeBlockCreate) -> dict[str, Any]:
        block_id = uuid4().hex
        with self.transaction() as connection:
            if data.kind == "busy":
                self._assert_no_scheduled_tasks(connection, Interval(data.start_at, data.end_at))
            connection.execute(
                "INSERT INTO time_blocks(id,kind,start_at_utc,end_at_utc,label) VALUES (?,?,?,?,?)",
                (block_id, data.kind, _timestamp(data.start_at), _timestamp(data.end_at), data.label),
            )
        return self.get_time_block(block_id)

    def get_time_block(self, block_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM time_blocks WHERE id=?", (block_id,)).fetchone()
        if row is None:
            raise KeyError(block_id)
        return dict(row)

    def list_time_blocks(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is not None and kind not in {"available", "busy"}:
            raise ValueError("Invalid time block kind")
        sql = "SELECT * FROM time_blocks"
        params: tuple[Any, ...] = ()
        if kind is not None:
            sql += " WHERE kind=?"
            params = (kind,)
        with self.connection() as connection:
            return _rows(connection.execute(sql + " ORDER BY start_at_utc", params))

    def update_time_block(self, block_id: str, data: TimeBlockCreate,
                          *, allow_pending_replan: bool = False) -> dict[str, Any]:
        with self.transaction() as connection:
            if data.kind == "busy":
                self._assert_no_scheduled_tasks(connection, Interval(data.start_at, data.end_at))
            cursor = connection.execute(
                "UPDATE time_blocks SET kind=?,start_at_utc=?,end_at_utc=?,label=? WHERE id=?",
                (data.kind, _timestamp(data.start_at), _timestamp(data.end_at), data.label, block_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(block_id)
            if not allow_pending_replan:
                self._assert_scheduled_tasks_fit_availability(connection)
        return self.get_time_block(block_id)

    def delete_time_block(self, block_id: str,
                          *, allow_pending_replan: bool = False) -> None:
        with self.transaction() as connection:
            if connection.execute("DELETE FROM time_blocks WHERE id=?", (block_id,)).rowcount == 0:
                raise KeyError(block_id)
            if not allow_pending_replan:
                self._assert_scheduled_tasks_fit_availability(connection)

    def create_task(self, data: TaskCreate) -> dict[str, Any]:
        task_id = uuid4().hex
        with self.transaction() as connection:
            self._assert_task_slot(connection, data)
            connection.execute(
                "INSERT INTO tasks(id,goal_id,title,status,priority,estimated_minutes,"
                "due_at_utc,start_at_utc,end_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task_id, data.goal_id, data.title, "pending", data.priority,
                    data.estimated_minutes, _timestamp(data.due_at),
                    _timestamp(data.start_at), _timestamp(data.end_at),
                ),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in {"pending", "in_progress", "completed"}:
            raise ValueError("Invalid task status")
        sql = "SELECT * FROM tasks"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status=?"
            params = (status,)
        with self.connection() as connection:
            return _rows(connection.execute(sql + " ORDER BY rowid", params))

    def update_task(self, task_id: str, data: TaskCreate,
                    expected_version: int | None = None) -> dict[str, Any]:
        with self.transaction() as connection:
            current = connection.execute("SELECT version FROM tasks WHERE id=?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(task_id)
            if expected_version is not None and current["version"] != expected_version:
                raise ValueError("stale_task_version")
            self._assert_task_slot(connection, data, exclude_task_id=task_id)
            changed = connection.execute(
                "UPDATE tasks SET goal_id=?,title=?,priority=?,estimated_minutes=?,"
                "due_at_utc=?,start_at_utc=?,end_at_utc=?,version=version+1 WHERE id=?"
                + (" AND version=?" if expected_version is not None else ""),
                (
                    data.goal_id, data.title, data.priority, data.estimated_minutes,
                    _timestamp(data.due_at), _timestamp(data.start_at),
                    _timestamp(data.end_at), task_id,
                    *((expected_version,) if expected_version is not None else ()),
                ),
            ).rowcount
            if not changed:
                raise ValueError("stale_task_version")
        return self.get_task(task_id)

    def set_task_status(self, task_id: str, status: str) -> dict[str, Any]:
        if status not in {"pending", "in_progress", "completed"}:
            raise ValueError("Invalid task status")
        with self.transaction() as connection:
            if connection.execute(
                "UPDATE tasks SET status=?,version=version+1 WHERE id=?", (status, task_id)
            ).rowcount == 0:
                raise KeyError(task_id)
        return self.get_task(task_id)

    def delete_task(self, task_id: str) -> None:
        with self.transaction() as connection:
            if connection.execute("DELETE FROM tasks WHERE id=?", (task_id,)).rowcount == 0:
                raise KeyError(task_id)

    def create_recurrence_rule(self, data: RecurrenceRuleCreate) -> dict[str, Any]:
        rule_id = uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO recurrence_rules(id,title,domain,frequency,weekdays_json,timezone,"
                "start_date,end_date,preferred_time,estimated_minutes,priority) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rule_id, data.title, data.domain, data.frequency,
                 json.dumps(sorted(data.weekdays)), data.timezone, data.start_date.isoformat(),
                 data.end_date.isoformat() if data.end_date else None,
                 data.preferred_time.isoformat() if data.preferred_time else None,
                 data.estimated_minutes, data.priority),
            )
        return self.get_recurrence_rule(rule_id)

    def confirm_recurrence_suggestion(
        self, run_id: str, revision: int, item_id: str,
        weekdays: list[int], start_date: date,
    ) -> dict[str, Any]:
        if len(weekdays) != 3 or len(set(weekdays)) != 3:
            raise ValueError("three_distinct_weekdays_required")
        rule_id = suggestion_rule_id(run_id, item_id)
        with self.transaction() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["kind"] != "planning" or run["status"] not in {"waiting_approval", "success"}:
                raise ValueError("plan_not_reviewable")
            if run["revision"] != revision:
                raise ValueError("stale_plan_revision")
            draft = PlanDraft.model_validate_json(run["draft_json"])
            snapshot = json.loads(run["config_snapshot_json"])
            timezone_name = snapshot.get("timezone") or "Australia/Sydney"
            suggestion = next((item for item in weekly_three_suggestions(
                run["request_text"], draft, timezone_name
            ) if item["item_id"] == item_id), None)
            if suggestion is None:
                raise ValueError("recurrence_suggestion_unavailable")
            data = RecurrenceRuleCreate(
                title=suggestion["title"], domain="life", frequency="weekly_days",
                weekdays=weekdays, timezone=timezone_name, start_date=start_date,
                estimated_minutes=suggestion["estimated_minutes"],
                priority=suggestion["priority"],
            )
            existing = connection.execute(
                "SELECT * FROM recurrence_rules WHERE id=?", (rule_id,)
            ).fetchone()
            if existing is not None:
                if (json.loads(existing["weekdays_json"]) != sorted(weekdays)
                    or existing["start_date"] != start_date.isoformat()):
                    raise ValueError("recurrence_suggestion_already_confirmed")
                return dict(existing)
            connection.execute(
                "INSERT INTO recurrence_rules(id,title,domain,frequency,weekdays_json,timezone,"
                "start_date,end_date,preferred_time,estimated_minutes,priority) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rule_id, data.title, data.domain, data.frequency,
                 json.dumps(sorted(data.weekdays)), data.timezone, data.start_date.isoformat(),
                 None, None, data.estimated_minutes, data.priority),
            )
        return self.get_recurrence_rule(rule_id)

    def get_recurrence_rule(self, rule_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM recurrence_rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise KeyError(rule_id)
        return dict(row)

    def list_recurrence_rules(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return _rows(connection.execute("SELECT * FROM recurrence_rules ORDER BY rowid"))

    def update_recurrence_rule(self, rule_id: str, data: RecurrenceRuleCreate) -> dict[str, Any]:
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE recurrence_rules SET title=?,domain=?,frequency=?,weekdays_json=?,"
                "timezone=?,start_date=?,end_date=?,preferred_time=?,estimated_minutes=?,"
                "priority=?,version=version+1 WHERE id=?",
                (data.title, data.domain, data.frequency, json.dumps(sorted(data.weekdays)),
                 data.timezone, data.start_date.isoformat(),
                 data.end_date.isoformat() if data.end_date else None,
                 data.preferred_time.isoformat() if data.preferred_time else None,
                 data.estimated_minutes, data.priority, rule_id),
            ).rowcount
            if not changed:
                raise KeyError(rule_id)
        return self.get_recurrence_rule(rule_id)

    def set_recurrence_rule_status(self, rule_id: str, status: str) -> dict[str, Any]:
        if status not in {"active", "paused"}:
            raise ValueError("invalid_recurrence_status")
        with self.transaction() as connection:
            if not connection.execute(
                "UPDATE recurrence_rules SET status=?,version=version+1 WHERE id=?",
                (status, rule_id),
            ).rowcount:
                raise KeyError(rule_id)
        return self.get_recurrence_rule(rule_id)

    def list_recurrence_instances(self, rule_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if rule_id is None:
                return _rows(connection.execute("SELECT * FROM recurrence_instances ORDER BY local_date,rowid"))
            return _rows(connection.execute(
                "SELECT * FROM recurrence_instances WHERE rule_id=? ORDER BY local_date,rowid",
                (rule_id,),
            ))

    def create_habit(self, data: HabitCreate) -> dict[str, Any]:
        habit_id = uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO habits(id,title,timezone,target_per_week) VALUES (?,?,?,?)",
                (habit_id, data.title, data.timezone, data.target_per_week),
            )
        return self.get_habit(habit_id)

    def get_habit(self, habit_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM habits WHERE id=?", (habit_id,)).fetchone()
        if row is None:
            raise KeyError(habit_id)
        return dict(row)

    def list_habits(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return _rows(connection.execute("SELECT * FROM habits ORDER BY rowid"))

    def update_habit(self, habit_id: str, data: HabitCreate) -> dict[str, Any]:
        with self.transaction() as connection:
            if not connection.execute(
                "UPDATE habits SET title=?,timezone=?,target_per_week=?,version=version+1 WHERE id=?",
                (data.title, data.timezone, data.target_per_week, habit_id),
            ).rowcount:
                raise KeyError(habit_id)
        return self.get_habit(habit_id)

    def set_habit_status(self, habit_id: str, status: str) -> dict[str, Any]:
        if status not in {"active", "paused"}:
            raise ValueError("invalid_habit_status")
        with self.transaction() as connection:
            if not connection.execute(
                "UPDATE habits SET status=?,version=version+1 WHERE id=?", (status, habit_id)
            ).rowcount:
                raise KeyError(habit_id)
        return self.get_habit(habit_id)

    def checkin_habit(self, habit_id: str, data: HabitCheckin) -> dict[str, Any]:
        with self.transaction() as connection:
            habit = connection.execute("SELECT status FROM habits WHERE id=?", (habit_id,)).fetchone()
            if habit is None:
                raise KeyError(habit_id)
            if habit["status"] != "active":
                raise ValueError("habit_paused")
            connection.execute(
                "INSERT INTO habit_checkins(id,habit_id,local_date,completed,note,updated_at_utc) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(habit_id,local_date) DO UPDATE SET "
                "completed=excluded.completed,note=excluded.note,updated_at_utc=excluded.updated_at_utc",
                (uuid4().hex, habit_id, data.local_date.isoformat(), int(data.completed),
                 data.note, _now()),
            )
            row = connection.execute(
                "SELECT * FROM habit_checkins WHERE habit_id=? AND local_date=?",
                (habit_id, data.local_date.isoformat()),
            ).fetchone()
            return dict(row)

    def list_habit_checkins(self, habit_id: str) -> list[dict[str, Any]]:
        self.get_habit(habit_id)
        with self.connection() as connection:
            return _rows(connection.execute(
                "SELECT * FROM habit_checkins WHERE habit_id=? ORDER BY local_date DESC", (habit_id,)
            ))

    def list_feedback(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return _rows(connection.execute("SELECT * FROM feedback ORDER BY created_at_utc DESC"))

    def create_feedback_run(self, task_id: str, body: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not body.strip():
            raise ValueError("feedback_body_empty")
        run_id, feedback_id = uuid4().hex, uuid4().hex
        with self.transaction() as connection:
            task = connection.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["status"] != "completed":
                raise ValueError("feedback_requires_completed_task")
            connection.execute(
                "INSERT INTO runs(id,kind,request_text,idempotency_key,status) "
                "VALUES (?,'feedback',?,?,'pending')",
                (run_id, body.strip(), run_id),
            )
            connection.execute(
                "INSERT INTO feedback(id,task_id,run_id,body,created_at_utc) VALUES (?,?,?,?,?)",
                (feedback_id, task_id, run_id, body.strip(), _now()),
            )
        return self.get_feedback(feedback_id), self.get_run(run_id)

    def get_feedback(self, feedback_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM feedback WHERE id=?", (feedback_id,)).fetchone()
        if row is None:
            raise KeyError(feedback_id)
        return dict(row)

    def feedback_for_run(self, run_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM feedback WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def save_memory_proposals(
        self, run_id: str, feedback_id: str, proposals: list[MemoryProposal]
    ) -> list[dict[str, Any]]:
        ids: list[str] = []
        with self.transaction() as connection:
            run = connection.execute("SELECT kind,status FROM runs WHERE id=?", (run_id,)).fetchone()
            feedback = connection.execute(
                "SELECT run_id FROM feedback WHERE id=?", (feedback_id,)
            ).fetchone()
            if run is None or run["kind"] != "feedback" or run["status"] != "running":
                raise ValueError("feedback_run_not_running")
            if feedback is None or feedback["run_id"] != run_id:
                raise ValueError("proposal_feedback_mismatch")
            for proposal in proposals:
                if proposal.feedback_id != feedback_id:
                    raise ValueError("proposal_feedback_mismatch")
                proposal_id = uuid4().hex
                ids.append(proposal_id)
                connection.execute(
                    "INSERT INTO memory_proposals(id,feedback_id,kind,value_json,source_excerpt,explanation,status) "
                    "VALUES (?,?,?,?,?,?,'pending')",
                    (proposal_id, feedback_id, proposal.kind,
                     json.dumps(proposal.value, ensure_ascii=False),
                     proposal.source_excerpt, proposal.explanation),
                )
            status = "waiting_approval" if ids else "success"
            connection.execute(
                "UPDATE runs SET status=?,ended_at_utc=? WHERE id=?",
                (status, _now() if not ids else None, run_id),
            )
        return [self.get_memory_proposal(item) for item in ids]

    def get_memory_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return dict(row)

    def list_memory_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in {"pending", "approved", "rejected"}:
            raise ValueError("invalid_proposal_status")
        sql = "SELECT * FROM memory_proposals"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status=?"
            params = (status,)
        with self.connection() as connection:
            return _rows(connection.execute(sql + " ORDER BY rowid", params))

    def edit_memory_proposal(self, proposal_id: str, value: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT kind,status FROM memory_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] != "pending":
                raise ValueError("proposal_already_resolved")
            checked = validate_memory_value(row["kind"], value)
            connection.execute(
                "UPDATE memory_proposals SET value_json=? WHERE id=?",
                (json.dumps(checked, ensure_ascii=False), proposal_id),
            )
        return self.get_memory_proposal(proposal_id)

    def resolve_memory_proposal(self, proposal_id: str, approve: bool) -> dict[str, Any] | None:
        memory_id = uuid4().hex if approve else None
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT p.*,f.run_id FROM memory_proposals p "
                "JOIN feedback f ON f.id=p.feedback_id WHERE p.id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] != "pending":
                raise ValueError("proposal_already_resolved")
            run = connection.execute(
                "SELECT status FROM runs WHERE id=?", (row["run_id"],)
            ).fetchone()
            if run is None or run["status"] != "waiting_approval":
                raise ValueError("feedback_run_not_waiting_approval")
            if approve:
                checked = validate_memory_value(row["kind"], json.loads(row["value_json"]))
                connection.execute(
                    "INSERT INTO memories(id,source_feedback_id,kind,value_json,status,created_at_utc,updated_at_utc) "
                    "VALUES (?,?,?,?,'approved',?,?)",
                    (memory_id, row["feedback_id"], row["kind"],
                     json.dumps(checked, ensure_ascii=False), _now(), _now()),
                )
            connection.execute(
                "UPDATE memory_proposals SET status=? WHERE id=?",
                ("approved" if approve else "rejected", proposal_id),
            )
            pending = connection.execute(
                "SELECT COUNT(*) FROM memory_proposals p JOIN feedback f ON f.id=p.feedback_id "
                "WHERE f.run_id=? AND p.status='pending'", (row["run_id"],)
            ).fetchone()[0]
            if pending == 0:
                connection.execute(
                    "UPDATE runs SET status='success',ended_at_utc=? "
                    "WHERE id=? AND status='waiting_approval'", (_now(), row["run_id"]),
                )
        return self.get_memory(memory_id) if memory_id else None

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return dict(row)

    def update_memory(self, memory_id: str, value: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT kind,status FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
            if row is None:
                raise KeyError(memory_id)
            if row["status"] != "approved":
                raise ValueError("memory_deleted")
            checked = validate_memory_value(row["kind"], value)
            connection.execute(
                "UPDATE memories SET value_json=?,updated_at_utc=? WHERE id=?",
                (json.dumps(checked, ensure_ascii=False), _now(), memory_id),
            )
        return self.get_memory(memory_id)

    def delete_memory(self, memory_id: str) -> None:
        with self.transaction() as connection:
            if connection.execute(
                "UPDATE memories SET status='deleted',updated_at_utc=? "
                "WHERE id=? AND status='approved'", (_now(), memory_id),
            ).rowcount == 0:
                raise KeyError(memory_id)

    def list_approved_memories(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return _rows(connection.execute("SELECT * FROM memories WHERE status='approved' ORDER BY updated_at_utc DESC"))

    def list_agent_configs(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return _rows(connection.execute("SELECT * FROM agent_configs ORDER BY role"))

    def get_agent_config(self, role: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM agent_configs WHERE role=?", (role,)).fetchone()
        if row is None:
            raise KeyError(role)
        return dict(row)

    def insert_agent_config(self, role: str, description: str, instructions: str, tools: set[str]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO agent_configs(role,description,instructions,tool_subset_json)"
                " VALUES (?,?,?,?)",
                (role, description, instructions, json.dumps(sorted(tools))),
            )

    def update_agent_config(self, role: str, instructions: str, tools: set[str]) -> dict[str, Any]:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE agent_configs SET instructions=?,tool_subset_json=?,version=version+1 WHERE role=?",
                (instructions, json.dumps(sorted(tools)), role),
            )
            if cursor.rowcount == 0:
                raise KeyError(role)
        return self.get_agent_config(role)

    @staticmethod
    def _custom_agent_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["tool_subset"] = sorted(json.loads(result.pop("tool_subset_json")))
        return result

    def list_custom_agents(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM custom_agents ORDER BY name COLLATE NOCASE").fetchall()
        return [self._custom_agent_row(row) for row in rows]

    def get_custom_agent(self, agent_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM custom_agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            raise KeyError(agent_id)
        return self._custom_agent_row(row)

    def create_custom_agent(self, data: CustomAgentCreate) -> dict[str, Any]:
        agent_id = uuid4().hex
        now = _now()
        try:
            with self.transaction() as connection:
                connection.execute(
                    "INSERT INTO custom_agents(id,name,description,instructions,tool_subset_json,"
                    "status,version,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,'paused',1,?,?)",
                    (agent_id, data.name, data.description, data.instructions,
                     json.dumps(sorted(data.tool_subset)), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate_custom_agent_name") from exc
        return self.get_custom_agent(agent_id)

    def update_custom_agent(
        self, agent_id: str, expected_version: int, data: CustomAgentCreate,
    ) -> dict[str, Any]:
        try:
            with self.transaction() as connection:
                cursor = connection.execute(
                    "UPDATE custom_agents SET name=?,description=?,instructions=?,"
                    "tool_subset_json=?,version=version+1,updated_at_utc=? "
                    "WHERE id=? AND version=?",
                    (data.name, data.description, data.instructions,
                     json.dumps(sorted(data.tool_subset)), _now(), agent_id, expected_version),
                )
                if cursor.rowcount == 0:
                    if connection.execute("SELECT 1 FROM custom_agents WHERE id=?", (agent_id,)).fetchone():
                        raise ValueError("stale_custom_agent_version")
                    raise KeyError(agent_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate_custom_agent_name") from exc
        return self.get_custom_agent(agent_id)

    def set_custom_agent_status(
        self, agent_id: str, expected_version: int, status: str,
    ) -> dict[str, Any]:
        if status not in {"active", "paused"}:
            raise ValueError("invalid_custom_agent_status")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE custom_agents SET status=?,version=version+1,updated_at_utc=? "
                "WHERE id=? AND version=?",
                (status, _now(), agent_id, expected_version),
            )
            if cursor.rowcount == 0:
                if connection.execute("SELECT 1 FROM custom_agents WHERE id=?", (agent_id,)).fetchone():
                    raise ValueError("stale_custom_agent_version")
                raise KeyError(agent_id)
        return self.get_custom_agent(agent_id)

    def create_run(
        self, request_text: str, *, kind: str = "planning",
        conversation_id: str | None = None, idempotency_key: str | None = None,
        config_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if kind not in {"planning", "feedback"} or not request_text.strip():
            raise ValueError("Invalid run request")
        run_id = uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO runs(id,kind,conversation_id,request_text,idempotency_key,status,config_snapshot_json) "
                "VALUES (?,?,?,?,?,'pending',?)",
                (run_id, kind, conversation_id, request_text.strip(),
                 idempotency_key or run_id, json.dumps(config_snapshot or {}, ensure_ascii=False)),
            )
        return self.get_run(run_id)

    def create_retry_run(self, failed_run_id: str) -> dict[str, Any]:
        """A retry is a new run; failed source and its feedback remain immutable."""
        retry_id = uuid4().hex
        with self.transaction() as connection:
            source = connection.execute(
                "SELECT * FROM runs WHERE id=?", (failed_run_id,)
            ).fetchone()
            if source is None:
                raise KeyError(failed_run_id)
            if source["status"] != "failed":
                raise ValueError("only_failed_runs_can_retry")
            connection.execute(
                "INSERT INTO runs(id,kind,conversation_id,request_text,idempotency_key,status,"
                "config_snapshot_json,retry_of_run_id) VALUES (?,?,?,?,?,'pending','{}',?)",
                (retry_id, source["kind"], source["conversation_id"], source["request_text"],
                 retry_id, failed_run_id),
            )
            if source["kind"] == "feedback":
                feedback = connection.execute(
                    "SELECT * FROM feedback WHERE run_id=?", (failed_run_id,)
                ).fetchone()
                if feedback is None:
                    raise ValueError("retry_feedback_missing")
                task = connection.execute(
                    "SELECT status FROM tasks WHERE id=?", (feedback["task_id"],)
                ).fetchone()
                if task is None or task["status"] != "completed":
                    raise ValueError("feedback_requires_completed_task")
                connection.execute(
                    "INSERT INTO feedback(id,task_id,run_id,body,created_at_utc) VALUES (?,?,?,?,?)",
                    (uuid4().hex, feedback["task_id"], retry_id, feedback["body"], _now()),
                )
        return self.get_run(retry_id)

    def read_state_fingerprint(self) -> str:
        """Cover every business table a planning read tool can see, without storing raw data."""
        with self.connection() as connection:
            connection.execute("BEGIN")
            state = {
                table: _rows(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                for table in (
                    "goals", "tasks", "time_blocks", "settings", "memories",
                    "feedback", "agent_configs",
                )
            }
        serialized = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()

    def save_validated_checkpoint(
        self, run_id: str, step_id: str, role: str, schema_name: str,
        input_fingerprint: str, output: dict[str, Any], result: dict[str, Any],
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE run_steps SET status='success',result_json=?,error_code=NULL "
                "WHERE run_id=? AND step_id=? AND status='running'",
                (json.dumps(result, ensure_ascii=False), run_id, step_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("step_not_running")
            connection.execute(
                "INSERT INTO run_checkpoints(id,run_id,step_id,role,schema_name,"
                "input_fingerprint,output_json,created_at_utc) VALUES (?,?,?,?,?,?,?,?)",
                (uuid4().hex, run_id, step_id, role, schema_name, input_fingerprint,
                 json.dumps(output, ensure_ascii=False), _now()),
            )

    def find_checkpoint(
        self, source_run_id: str | None, step_id: str, role: str,
        schema_name: str, input_fingerprint: str,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            seen: set[str] = set()
            candidate = source_run_id
            while candidate and candidate not in seen:
                seen.add(candidate)
                row = connection.execute(
                    "SELECT * FROM run_checkpoints WHERE run_id=? AND step_id=? "
                    "AND role=? AND schema_name=? AND input_fingerprint=?",
                    (candidate, step_id, role, schema_name, input_fingerprint),
                ).fetchone()
                if row:
                    return dict(row)
                ancestor = connection.execute(
                    "SELECT retry_of_run_id FROM runs WHERE id=?", (candidate,)
                ).fetchone()
                candidate = ancestor["retry_of_run_id"] if ancestor else None
        return None

    def record_model_usage(
        self, run_id: str, step_id: str, attempt: int,
        requests: list[dict[str, Any]], error_code: str | None,
    ) -> None:
        with self.transaction() as connection:
            for index, request in enumerate(requests, start=1):
                connection.execute(
                    "INSERT INTO model_usage(id,run_id,step_id,attempt,request_index,model_id,"
                    "input_tokens,output_tokens,total_tokens,duration_ms,error_code,created_at_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid4().hex, run_id, step_id, attempt, index, request["model_id"],
                     request.get("input_tokens"), request.get("output_tokens"),
                     request.get("total_tokens"), request["duration_ms"], error_code, _now()),
                )

    def list_model_usage(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if run_id is None:
                return _rows(connection.execute("SELECT * FROM model_usage ORDER BY rowid"))
            return _rows(connection.execute(
                "SELECT * FROM model_usage WHERE run_id=? ORDER BY rowid", (run_id,)
            ))

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def list_runs(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return _rows(connection.execute("SELECT * FROM runs ORDER BY rowid DESC"))

    def set_run_config_snapshot(self, run_id: str, snapshot: dict[str, Any]) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE runs SET config_snapshot_json=? WHERE id=? "
                "AND status IN ('pending','running') AND draft_json IS NULL",
                (json.dumps(snapshot, ensure_ascii=False), run_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("run_not_pending")

    def update_run_status(
        self, run_id: str, status: str, *, draft: dict[str, Any] | None = None
    ) -> None:
        if status not in {"running", "waiting_approval", "success", "failed", "cancelled"}:
            raise ValueError("Invalid run status")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status=?, draft_json=COALESCE(?,draft_json), "
                "started_at_utc=CASE WHEN ?='running' THEN COALESCE(started_at_utc,?) ELSE started_at_utc END, "
                "ended_at_utc=CASE WHEN ? IN ('success','failed','cancelled') THEN ? ELSE ended_at_utc END "
                "WHERE id=?",
                (status, json.dumps(draft, ensure_ascii=False) if draft is not None else None,
                 status, _now(), status, _now(), run_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(run_id)

    def edit_plan_draft(
        self, run_id: str, revision: int, tasks: list[TaskDraft], timezone_name: str
    ) -> PlanDraft:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["kind"] != "planning" or row["status"] != "waiting_approval":
                raise ValueError("plan_not_waiting_approval")
            if row["revision"] != revision:
                raise ValueError("stale_plan_revision")
            original = PlanDraft.model_validate_json(row["draft_json"])
            existing = {item.draft_item_id: item for item in original.tasks}
            ids = [item.draft_item_id for item in tasks]
            if not tasks or len(ids) != len(set(ids)) or not set(ids) <= set(existing):
                raise ValueError("invalid_edited_task_set")
            if any(
                item.source_step_id != existing[item.draft_item_id].source_step_id
                or item.domain != existing[item.draft_item_id].domain
                or item.resource_url != existing[item.draft_item_id].resource_url
                or item.resource_source != existing[item.draft_item_id].resource_source
                or item.resource_channel != existing[item.draft_item_id].resource_channel
                or item.resource_duration_seconds != existing[item.draft_item_id].resource_duration_seconds
                for item in tasks
            ):
                raise ValueError("task_source_cannot_change")
            conflicts = self._draft_conflicts(connection, tasks, timezone_name)
            edited = PlanDraft.model_validate(original.model_copy(update={
                "revision": revision + 1, "tasks": tasks, "conflicts": conflicts,
            }).model_dump())
            connection.execute(
                "UPDATE runs SET draft_json=?,revision=? WHERE id=?",
                (edited.model_dump_json(), edited.revision, run_id),
            )
        return edited

    def _draft_conflicts(
        self, connection: sqlite3.Connection,
        tasks: list[TaskDraft], timezone_name: str,
    ) -> list[str]:
        blocks = _rows(connection.execute("SELECT * FROM time_blocks"))
        def interval(row: dict[str, Any]) -> Interval:
            return Interval(
                datetime.fromisoformat(row["start_at_utc"]),
                datetime.fromisoformat(row["end_at_utc"]),
            )
        available = [interval(row) for row in blocks if row["kind"] == "available"]
        busy = [interval(row) for row in blocks if row["kind"] == "busy"]
        occupied = [interval(row) for row in _rows(connection.execute(
            "SELECT * FROM tasks WHERE start_at_utc IS NOT NULL AND status!='completed'"
        ))]
        avoid, _ = approved_study_avoid_ranges(_rows(connection.execute(
            "SELECT * FROM memories WHERE status='approved'"
        )))
        conflicts: list[str] = []
        for item in tasks:
            if item.start_at is None:
                conflicts.append(f"{item.draft_item_id}: unassigned")
                continue
            candidate = Interval(item.start_at, item.end_at)
            issues = validate_schedule(
                candidate, availability=available, busy=busy, tasks=occupied,
                due_at=item.due_at, avoid_local_ranges=avoid if item.domain == "study" else [],
                timezone_name=timezone_name,
            )
            if (item.end_at - item.start_at).total_seconds() < item.estimated_minutes * 60:
                issues.append("insufficient_duration")
            conflicts.extend(f"{item.draft_item_id}: {issue}" for issue in issues)
            if not issues:
                occupied.append(candidate)
        return conflicts

    def reject_plan(self, run_id: str, revision: int) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT kind,status,revision FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["kind"] != "planning" or row["status"] != "waiting_approval":
                raise ValueError("plan_not_waiting_approval")
            if row["revision"] != revision:
                raise ValueError("stale_plan_revision")
            connection.execute(
                "UPDATE runs SET status='cancelled',ended_at_utc=? WHERE id=?",
                (_now(), run_id),
            )

    def commit_approved_plan(
        self, run_id: str, revision: int, timezone_name: str
    ) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["kind"] != "planning" or run["revision"] != revision:
                raise ValueError("stale_plan_revision")
            if run["status"] == "success":
                return _rows(connection.execute(
                    "SELECT * FROM tasks WHERE source_run_id=? ORDER BY rowid", (run_id,)
                ))
            if run["status"] != "waiting_approval":
                raise ValueError("plan_not_waiting_approval")
            draft = PlanDraft.model_validate_json(run["draft_json"])
            if draft.run_id != run_id or draft.revision != revision or not draft.tasks:
                raise ValueError("invalid_plan_draft")
            ids = [item.draft_item_id for item in draft.tasks]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate_draft_item")
            valid_steps = {
                row["step_id"]: row["agent"] for row in connection.execute(
                    "SELECT step_id,agent FROM run_steps WHERE run_id=? AND status='success'", (run_id,)
                )
            }
            if any(
                valid_steps.get(item.source_step_id) != ("learning" if item.domain == "study" else "life")
                for item in draft.tasks
            ):
                raise ValueError("invalid_task_source_step")

            memory_rows = _rows(connection.execute("SELECT * FROM memories WHERE status='approved'"))
            active_memory_ids = {item["id"] for item in memory_rows}
            if not set(draft.memory_ids) <= active_memory_ids:
                raise ValueError("referenced_memory_changed")
            avoid_ranges, _ = approved_study_avoid_ranges(memory_rows)
            blocks = _rows(connection.execute("SELECT * FROM time_blocks"))
            def interval(row: dict[str, Any]) -> Interval:
                return Interval(
                    datetime.fromisoformat(row["start_at_utc"]),
                    datetime.fromisoformat(row["end_at_utc"]),
                )
            available = [interval(row) for row in blocks if row["kind"] == "available"]
            busy = [interval(row) for row in blocks if row["kind"] == "busy"]
            occupied = [
                interval(row) for row in _rows(connection.execute(
                    "SELECT * FROM tasks WHERE start_at_utc IS NOT NULL AND status!='completed'"
                ))
            ]
            for item in draft.tasks:
                if item.start_at is not None:
                    candidate = Interval(item.start_at, item.end_at)
                    issues = validate_schedule(
                        candidate, availability=available, busy=busy, tasks=occupied,
                        due_at=item.due_at,
                        avoid_local_ranges=avoid_ranges if item.domain == "study" else [],
                        timezone_name=timezone_name,
                    )
                    if (item.end_at - item.start_at).total_seconds() < item.estimated_minutes * 60:
                        issues.append("insufficient_duration")
                    if issues:
                        raise ValueError("schedule_conflict:" + ",".join(issues))
                    occupied.append(candidate)
                connection.execute(
                    "INSERT INTO tasks(id,goal_id,title,status,priority,estimated_minutes,"
                    "due_at_utc,start_at_utc,end_at_utc,source_run_id,draft_item_id,domain,"
                    "resource_url,resource_source,resource_channel,resource_duration_seconds) "
                    "VALUES (?,NULL,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid4().hex, item.title, item.priority, item.estimated_minutes,
                     _timestamp(item.due_at), _timestamp(item.start_at),
                     _timestamp(item.end_at), run_id, item.draft_item_id, item.domain,
                     item.resource_url, item.resource_source, item.resource_channel,
                     item.resource_duration_seconds),
                )
            connection.execute(
                "UPDATE runs SET status='success',ended_at_utc=? WHERE id=?",
                (_now(), run_id),
            )
            connection.execute(
                "INSERT INTO trace_events(id,run_id,step_id,actor,event_type,summary_json,created_at_utc) "
                "VALUES (?,?,NULL,'user','plan_committed',?,?)",
                (uuid4().hex, run_id,
                 json.dumps({"revision": revision, "task_count": len(draft.tasks)}), _now()),
            )
            return _rows(connection.execute(
                "SELECT * FROM tasks WHERE source_run_id=? ORDER BY rowid", (run_id,)
            ))

    def create_run_step(
        self, run_id: str, step_id: str, agent: str, depends_on: list[str]
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO run_steps(id,run_id,step_id,agent,depends_on_json,status) "
                "VALUES (?,?,?,?,?,'pending')",
                (uuid4().hex, run_id, step_id, agent, json.dumps(depends_on)),
            )

    def update_run_step(
        self, run_id: str, step_id: str, status: str, *,
        result: dict[str, Any] | None = None, error_code: str | None = None,
    ) -> None:
        if status not in {"running", "success", "failed", "skipped"}:
            raise ValueError("Invalid step status")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE run_steps SET status=?,result_json=?,error_code=? WHERE run_id=? AND step_id=?",
                (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error_code, run_id, step_id),
            )
            if cursor.rowcount == 0:
                raise KeyError((run_id, step_id))

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return _rows(connection.execute(
                "SELECT * FROM run_steps WHERE run_id=? ORDER BY rowid", (run_id,)
            ))

    def fail_interrupted_runs(self) -> int:
        with self.transaction() as connection:
            rows = _rows(connection.execute(
                "SELECT id FROM runs WHERE status='running' OR "
                "(status='pending' AND "
                "(config_snapshot_json LIKE '%requested_custom_steps%' OR "
                "EXISTS(SELECT 1 FROM run_steps WHERE run_steps.run_id=runs.id "
                "AND run_steps.step_id LIKE 'custom_%')))"
            ))
            for row in rows:
                connection.execute(
                    "UPDATE run_steps SET status='failed',error_code='interrupted' "
                    "WHERE run_id=? AND status='running'", (row["id"],),
                )
                connection.execute(
                    "UPDATE run_steps SET status='skipped',error_code='interrupted' "
                    "WHERE run_id=? AND status='pending'", (row["id"],),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',ended_at_utc=? WHERE id=?",
                    (_now(), row["id"]),
                )
        for row in rows:
            self.append_trace(
                "run_failed", {"reason": "interrupted"}, run_id=row["id"]
            )
        return len(rows)

    def append_trace(
        self,
        event_type: str,
        summary: dict[str, Any],
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        actor: str = "system",
        duration_ms: int | None = None,
    ) -> str:
        event_id = uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO trace_events(id,run_id,step_id,actor,event_type,summary_json,"
                "duration_ms,created_at_utc) VALUES (?,?,?,?,?,?,?,?)",
                (
                    event_id, run_id, step_id, actor, event_type,
                    json.dumps(summary, ensure_ascii=False), duration_ms, _now(),
                ),
            )
        return event_id

    def list_trace(self, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trace_events"
        params: tuple[Any, ...] = ()
        if run_id is not None:
            sql += " WHERE run_id=?"
            params = (run_id,)
        with self.connection() as connection:
            return _rows(connection.execute(sql + " ORDER BY created_at_utc,rowid", params))

    def _assert_no_scheduled_tasks(self, connection: sqlite3.Connection, candidate: Interval) -> None:
        for row in connection.execute(
            "SELECT start_at_utc,end_at_utc FROM tasks WHERE start_at_utc IS NOT NULL AND status!='completed'"
        ):
            existing = Interval(datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1]))
            if overlaps(candidate, existing):
                raise ValueError("busy_overlap_with_task")

    def _assert_scheduled_tasks_fit_availability(self, connection: sqlite3.Connection) -> None:
        windows = [
            Interval(datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1]))
            for row in connection.execute(
                "SELECT start_at_utc,end_at_utc FROM time_blocks WHERE kind='available'"
            )
        ]
        for row in connection.execute(
            "SELECT start_at_utc,end_at_utc FROM tasks "
            "WHERE start_at_utc IS NOT NULL AND status!='completed'"
        ):
            task = Interval(datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1]))
            if not any(
                to_utc(window.start) <= to_utc(task.start)
                and to_utc(task.end) <= to_utc(window.end)
                for window in windows
            ):
                raise ValueError("scheduled_task_outside_availability")

    def _assert_task_slot(
        self, connection: sqlite3.Connection, data: TaskCreate, exclude_task_id: str | None = None
    ) -> None:
        if data.start_at is None or data.end_at is None:
            return
        blocks = _rows(connection.execute("SELECT * FROM time_blocks"))
        rows = _rows(connection.execute(
            "SELECT id,start_at_utc,end_at_utc FROM tasks "
            "WHERE start_at_utc IS NOT NULL AND status!='completed'"
        ))
        def interval(row: dict[str, Any]) -> Interval:
            return Interval(
                datetime.fromisoformat(row["start_at_utc"]),
                datetime.fromisoformat(row["end_at_utc"]),
            )

        issues = validate_schedule(
            Interval(data.start_at, data.end_at),
            availability=[interval(row) for row in blocks if row["kind"] == "available"],
            busy=[interval(row) for row in blocks if row["kind"] == "busy"],
            tasks=[interval(row) for row in rows if row["id"] != exclude_task_id],
            due_at=data.due_at,
        )
        if issues:
            raise ValueError("schedule_conflict:" + ",".join(issues))
