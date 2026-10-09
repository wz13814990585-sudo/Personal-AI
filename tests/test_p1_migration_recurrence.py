import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from personal_ai_os import migrations
from personal_ai_os.agent_registry import AgentRegistry, ROLE_DEFINITIONS
from personal_ai_os.contracts import RecurrenceRuleCreate, TaskCreate, TimeBlockCreate
from personal_ai_os.recurrence import generate_instances, local_start
from personal_ai_os.storage import Repository, SCHEMA


def legacy_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.close()
    repository = Repository(path)
    for role, definition in ROLE_DEFINITIONS.items():
        if role != "life":
            repository.insert_agent_config(role, definition.description,
                                           definition.instructions, set(definition.maximum_tools))
    repository.update_agent_config("memory", "custom user instruction", {"search_memory"})
    task = repository.create_task(TaskCreate(title="old task"))
    run = repository.create_run("old run")
    with repository.transaction() as connection:
        connection.execute(
            "INSERT INTO memories(id,kind,value_json,status,created_at_utc,updated_at_utc) "
            "VALUES ('old-memory','text_preference','{\"text\":\"afternoon\"}','approved','x','x')"
        )
        connection.execute(
            "INSERT INTO run_steps(id,run_id,step_id,agent,depends_on_json,status) "
            "VALUES ('old-step',?,'memory','memory','[]','success')", (run["id"],)
        )
    repository.append_trace("old_event", {"kept": True}, run_id=run["id"])
    return task["id"], run["id"]


def test_legacy_migration_backups_preserves_records_and_is_versioned(tmp_path):
    path = tmp_path / "old.sqlite3"
    task_id, run_id = legacy_database(path)
    repository = Repository(path)
    repository.initialize()
    backups = list(tmp_path.glob("old.backup-v0-*.sqlite3"))
    assert len(backups) == 1
    assert repository.get_task(task_id)["domain"] == "general"
    assert repository.get_task(task_id)["version"] == 1
    assert repository.update_task(task_id, TaskCreate(title="edited old task"))["version"] == 2
    assert repository.get_run(run_id)["id"] == run_id
    assert repository.list_approved_memories()[0]["id"] == "old-memory"
    assert len(repository.list_trace(run_id)) == 1
    assert repository.list_run_steps(run_id)[0]["id"] == "old-step"
    assert len(repository.list_agent_configs()) == 5
    assert repository.get_agent_config("memory")["instructions"] == "custom user instruction"
    assert repository.get_agent_config("memory")["version"] == 2
    with repository.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 0
        assert backup.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "old task"
        assert backup.execute("SELECT COUNT(*) FROM agent_configs").fetchone()[0] == 5
        assert backup.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0] == 1
    repository.initialize()
    assert len(list(tmp_path.glob("old.backup-v0-*.sqlite3"))) == 1


def test_failed_migration_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    path = tmp_path / "failed.sqlite3"
    task_id, _ = legacy_database(path)
    original = migrations.migrate_v1

    def fail_after_ddl(connection):
        original(connection)
        raise RuntimeError("injected migration error")

    monkeypatch.setattr(migrations, "migrate_v1", fail_after_ddl)
    with pytest.raises(RuntimeError, match="injected migration error"):
        Repository(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "domain" not in [row[1] for row in connection.execute("PRAGMA table_info(tasks)")]
        assert connection.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "old task"
    assert len(list(tmp_path.glob("failed.backup-v0-*.sqlite3"))) == 1
    monkeypatch.setattr(migrations, "migrate_v1", original)
    Repository(path).initialize()
    assert Repository(path).get_task(task_id)["domain"] == "general"


def test_daily_weekdays_pause_resume_idempotent_across_restart_and_competing_scans(tmp_path):
    path = tmp_path / "repeat.sqlite3"
    repository = Repository(path)
    repository.initialize()
    rule = repository.create_recurrence_rule(RecurrenceRuleCreate(
        title="exercise", domain="life", frequency="weekly_days", weekdays=[0, 2, 4],
        timezone="Australia/Perth", start_date=date(2026, 10, 5),
    ))
    first = generate_instances(repository, rule["id"], date(2026, 10, 5), date(2026, 10, 11),
                               as_of_local_date=date(2026, 10, 5))
    assert [row["local_date"] for row in first] == ["2026-10-05", "2026-10-07", "2026-10-09"]
    assert all(row["unassigned_reason"] == "no_preferred_time" for row in first)
    assert generate_instances(repository, rule["id"], date(2026, 10, 5), date(2026, 10, 11),
                              as_of_local_date=date(2026, 10, 5)) == []
    repository.set_recurrence_rule_status(rule["id"], "paused")
    assert generate_instances(repository, rule["id"], date(2026, 10, 12), date(2026, 10, 18),
                              as_of_local_date=date(2026, 10, 12)) == []
    repository.set_recurrence_rule_status(rule["id"], "active")
    reopened = Repository(path)
    reopened.initialize()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: generate_instances(reopened, rule["id"], date(2026, 10, 12),
                                         date(2026, 10, 18), as_of_local_date=date(2026, 10, 12)),
            range(2),
        ))
    assert sum(len(items) for items in results) == 3
    assert len(reopened.list_recurrence_instances(rule["id"])) == 6
    assert len([task for task in reopened.list_tasks() if task["recurrence_rule_id"] == rule["id"]]) == 6


def test_rule_zone_dst_and_no_legal_slot_have_explicit_reason(tmp_path):
    repository = Repository(tmp_path / "zones.sqlite3")
    repository.initialize()
    # Sydney 02:30 is absent at spring transition and appears twice in autumn.
    assert local_start(date(2026, 10, 4), datetime.strptime("02:30", "%H:%M").time(),
                       ZoneInfo("Australia/Sydney"))[1] == "nonexistent_local_time"
    assert local_start(date(2026, 4, 5), datetime.strptime("02:30", "%H:%M").time(),
                       ZoneInfo("Australia/Sydney"))[1] == "ambiguous_local_time"
    spring = repository.create_recurrence_rule(RecurrenceRuleCreate(
        title="spring", frequency="daily", timezone="Australia/Sydney",
        start_date=date(2026, 10, 4), preferred_time=datetime.strptime("02:30", "%H:%M").time(),
    ))
    created = generate_instances(repository, spring["id"], date(2026, 10, 4), date(2026, 10, 4),
                                 as_of_local_date=date(2026, 10, 4))
    assert created[0]["unassigned_reason"] == "nonexistent_local_time"
    assert repository.get_task(created[0]["task_id"])["start_at_utc"] is None
    autumn = repository.create_recurrence_rule(RecurrenceRuleCreate(
        title="autumn", frequency="daily", timezone="Australia/Sydney",
        start_date=date(2026, 4, 5), preferred_time=datetime.strptime("02:30", "%H:%M").time(),
    ))
    assert generate_instances(repository, autumn["id"], date(2026, 4, 5), date(2026, 4, 5),
                              as_of_local_date=date(2026, 4, 5))[0]["unassigned_reason"] == "ambiguous_local_time"
    perth = repository.create_recurrence_rule(RecurrenceRuleCreate(
        title="perth", frequency="daily", timezone="Australia/Perth",
        start_date=date(2026, 10, 4), preferred_time=datetime.strptime("02:30", "%H:%M").time(),
    ))
    assert generate_instances(repository, perth["id"], date(2026, 10, 4), date(2026, 10, 4),
                              as_of_local_date=date(2026, 10, 4))[0]["unassigned_reason"] == "no_availability"


def test_valid_rule_time_uses_rule_zone_and_conflict_check(tmp_path):
    repository = Repository(tmp_path / "schedule.sqlite3")
    repository.initialize()
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=datetime(2026, 10, 5, 1, tzinfo=timezone.utc),
        end_at=datetime(2026, 10, 5, 3, tzinfo=timezone.utc),
    ))
    rule = repository.create_recurrence_rule(RecurrenceRuleCreate(
        title="study", domain="study", frequency="daily", timezone="Australia/Perth",
        start_date=date(2026, 10, 5), preferred_time=datetime.strptime("09:00", "%H:%M").time(),
        estimated_minutes=60,
    ))
    first = generate_instances(repository, rule["id"], date(2026, 10, 5), date(2026, 10, 5),
                               as_of_local_date=date(2026, 10, 5))
    assert first[0]["unassigned_reason"] is None
    assert repository.get_task(first[0]["task_id"])["start_at_utc"] == "2026-10-05T01:00:00+00:00"
    second = repository.create_recurrence_rule(RecurrenceRuleCreate(
        title="overlap", frequency="daily", timezone="Australia/Perth",
        start_date=date(2026, 10, 5), preferred_time=datetime.strptime("09:00", "%H:%M").time(),
    ))
    assert generate_instances(repository, second["id"], date(2026, 10, 5), date(2026, 10, 5),
                              as_of_local_date=date(2026, 10, 5))[0]["unassigned_reason"] == "task_overlap"


def test_rule_edit_affects_future_only_and_deleted_task_does_not_regenerate(tmp_path):
    repository = Repository(tmp_path / "edit.sqlite3")
    repository.initialize()
    rule = repository.create_recurrence_rule(RecurrenceRuleCreate(
        title="first", frequency="daily", timezone="Australia/Perth",
        start_date=date(2026, 10, 5),
    ))
    first = generate_instances(repository, rule["id"], date(2026, 10, 5), date(2026, 10, 5),
                               as_of_local_date=date(2026, 10, 5))[0]
    repository.update_recurrence_rule(rule["id"], RecurrenceRuleCreate(
        title="second", frequency="daily", timezone="Australia/Sydney",
        start_date=date(2026, 10, 5),
    ))
    second = generate_instances(repository, rule["id"], date(2026, 10, 5), date(2026, 10, 6),
                                as_of_local_date=date(2026, 10, 5))
    assert len(second) == 1 and second[0]["local_date"] == "2026-10-06"
    assert repository.get_task(first["task_id"])["title"] == "first"
    assert repository.get_task(second[0]["task_id"])["title"] == "second"
    repository.delete_task(first["task_id"])
    assert generate_instances(repository, rule["id"], date(2026, 10, 5), date(2026, 10, 5),
                              as_of_local_date=date(2026, 10, 5)) == []
