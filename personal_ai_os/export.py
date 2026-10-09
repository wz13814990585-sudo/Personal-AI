"""Read-only local business-data export. Secrets and environment files are excluded."""

from __future__ import annotations

import csv
import io
import json

from .storage import Repository, _rows


TABLES = (
    "goals", "tasks", "time_blocks", "recurrence_rules", "recurrence_instances",
    "habits", "habit_checkins", "feedback", "memory_proposals", "memories",
    "daily_plan_proposals", "daily_reviews", "notifications",
)
SAFE_SETTINGS = frozenset({
    "timezone", "sleep_start", "sleep_end", "personal_goal", "study_preference",
    "quiet_start", "quiet_end", "system_notifications_enabled",
})
TASK_COLUMNS = (
    "id", "goal_id", "title", "status", "domain", "priority",
    "estimated_minutes", "due_at_utc", "start_at_utc", "end_at_utc",
    "source_run_id", "draft_item_id", "recurrence_rule_id", "version",
)


def export_json(repository: Repository) -> bytes:
    with repository.connection() as connection:
        connection.execute("BEGIN")
        data = {table: _rows(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                for table in TABLES}
        settings = _rows(connection.execute(
            "SELECT key,value_json FROM settings ORDER BY key"
        ))
        data["settings"] = {
            row["key"]: json.loads(row["value_json"])
            for row in settings if row["key"] in SAFE_SETTINGS
        }
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def export_tasks_csv(repository: Repository) -> bytes:
    with repository.connection() as connection:
        connection.execute("BEGIN")
        tasks = _rows(connection.execute("SELECT * FROM tasks ORDER BY rowid"))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=TASK_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(tasks)
    return output.getvalue().encode("utf-8")
