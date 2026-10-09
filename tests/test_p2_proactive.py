"""Grounded suggestions, dedupe, review and immutable strategy rollback."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from personal_ai_os import migrations
from personal_ai_os import worker as worker_module
from personal_ai_os.contracts import HabitCheckin, HabitCreate, TaskCreate
from personal_ai_os.proactive import current_strategy, list_suggestions, scan_suggestions
from personal_ai_os.storage import Repository
from personal_ai_os.worker import Worker, create_daily_review


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def memories(repo):
    with repo.transaction() as connection:
        connection.execute(
            "INSERT INTO memories(id,kind,value_json,status,created_at_utc,updated_at_utc) "
            "VALUES ('approved-study','text_preference',?, 'approved',?,?)",
            (json.dumps({"text": "study in afternoon"}), NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO memories(id,kind,value_json,status,created_at_utc,updated_at_utc) "
            "VALUES ('deleted-study','text_preference',?, 'deleted',?,?)",
            (json.dumps({"text": "study at dawn"}), NOW.isoformat(), NOW.isoformat()),
        )


def test_grounded_dedupe_reject_and_restart(flow_system):
    repo, _, _, _, _, service = flow_system
    memories(repo)
    task = service.create_task(TaskCreate(
        title="Study for interview", due_at=NOW - timedelta(days=3),
    ))
    before = (repo.list_tasks(), repo.list_approved_memories(), repo.list_time_blocks())
    created = scan_suggestions(repo, NOW)
    assert len(created) == 1
    suggestion = created[0]
    assert suggestion["kind"] == "overdue_task"
    assert task["id"] in suggestion["source_ids"]
    assert "approved-study" in suggestion["source_ids"]
    assert "deleted-study" not in suggestion["source_ids"]
    assert suggestion["generated_at_utc"] == NOW.isoformat()
    assert suggestion["dedupe_key"]
    assert scan_suggestions(repo, NOW) == []
    rejected = service.resolve_proactive_suggestion(suggestion["id"], 0, False)
    assert rejected["status"] == "rejected"
    assert scan_suggestions(repo, NOW) == []
    reopened = Repository(repo.path)
    reopened.initialize()
    assert scan_suggestions(reopened, NOW) == []
    assert service.current_suggestion_strategy()["version"] == 1
    assert (repo.list_tasks(), repo.list_approved_memories(), repo.list_time_blocks()) == before


def test_edit_accept_stale_evidence_and_rollback(flow_system):
    repo, _, _, _, _, service = flow_system
    task = service.create_task(TaskCreate(
        title="Prepare slides", due_at=NOW - timedelta(days=3),
    ))
    suggestion = scan_suggestions(repo, NOW)[0]
    edited = service.edit_proactive_suggestion(
        suggestion["id"], 0, "Review slides carefully", "User reviewed proposal", 0,
    )
    assert edited["revision"] == 1
    with pytest.raises(ValueError, match="stale_suggestion_revision"):
        service.resolve_proactive_suggestion(suggestion["id"], 0, True, as_of=NOW)
    with pytest.raises(ValueError, match="stale_suggestion_revision"):
        service.edit_proactive_suggestion(suggestion["id"], 0, "Old", "Old", 0)
    before = (repo.list_tasks(), repo.list_approved_memories(), repo.list_time_blocks())
    accepted = service.resolve_proactive_suggestion(suggestion["id"], 1, True, as_of=NOW)
    assert accepted["status"] == "accepted" and accepted["accepted_strategy_version"] == 2
    assert service.current_suggestion_strategy() == {
        "version": 2,
        "policy": {"overdue_days": 0, "habit_gap_min": 2, "review_unfinished_min": 2},
    }
    assert [item["version"] for item in service.list_suggestion_strategy_versions()] == [2, 1]
    with pytest.raises(ValueError, match="stale_suggestion_revision"):
        service.resolve_proactive_suggestion(suggestion["id"], 1, True, as_of=NOW)
    with pytest.raises(ValueError, match="stale_strategy_version"):
        service.rollback_suggestion_strategy(1, 1)
    restored = service.rollback_suggestion_strategy(1, 2)
    assert restored["version"] == 3
    assert restored["policy"]["overdue_days"] == 1
    versions = service.list_suggestion_strategy_versions()
    assert versions[0]["rollback_target_version"] == 1 and versions[0]["parent_version"] == 2
    assert current_strategy(Repository(repo.path))["version"] == 3
    assert (repo.list_tasks(), repo.list_approved_memories(), repo.list_time_blocks()) == before
    assert repo.get_task(task["id"])["status"] == "pending"
    assert [event["event_type"] for event in repo.list_trace() if event["event_type"].startswith("proactive_")] == [
        "proactive_suggestions_created", "proactive_suggestion_edited",
        "proactive_suggestion_accepted", "proactive_strategy_rolled_back",
    ]


def test_strategy_change_and_rollback_change_future_signals(flow_system):
    repo, _, _, _, _, service = flow_system
    recently_overdue = repo.create_task(TaskCreate(
        title="Recently overdue", due_at=NOW - timedelta(hours=12),
    ))
    assert scan_suggestions(repo, NOW) == []
    old = repo.create_task(TaskCreate(
        title="Long overdue", due_at=NOW - timedelta(days=3),
    ))
    suggestion = scan_suggestions(repo, NOW)[0]
    service.resolve_proactive_suggestion(suggestion["id"], 0, True, as_of=NOW)
    newly_found = scan_suggestions(repo, NOW)
    assert len(newly_found) == 1 and newly_found[0]["subject_id"] == recently_overdue["id"]
    service.rollback_suggestion_strategy(1, 2)
    another_recent = repo.create_task(TaskCreate(
        title="Another recent task", due_at=NOW - timedelta(hours=12),
    ))
    assert scan_suggestions(repo, NOW) == []
    assert repo.get_task(old["id"])["status"] == "pending"
    assert repo.get_task(another_recent["id"])["status"] == "pending"


def test_second_pending_suggestion_can_be_reviewed_after_strategy_change(flow_system):
    repo, _, _, _, _, service = flow_system
    for title in ("First overdue", "Second overdue"):
        repo.create_task(TaskCreate(title=title, due_at=NOW - timedelta(days=3)))
    pending = scan_suggestions(repo, NOW)
    assert len(pending) == 2 and {item["strategy_version"] for item in pending} == {1}
    service.resolve_proactive_suggestion(pending[0]["id"], 0, True, as_of=NOW)
    second = service.resolve_proactive_suggestion(pending[1]["id"], 0, True, as_of=NOW)
    assert second["status"] == "accepted" and second["accepted_strategy_version"] == 2
    assert len(service.list_suggestion_strategy_versions()) == 2


def test_changed_or_deleted_sources_block_acceptance(flow_system):
    repo, _, _, _, _, service = flow_system
    memories(repo)
    task = service.create_task(TaskCreate(title="Study for interview", due_at=NOW - timedelta(days=3)))
    suggestion = scan_suggestions(repo, NOW)[0]
    service.delete_memory("approved-study")
    with pytest.raises(ValueError, match="stale_suggestion_evidence"):
        service.resolve_proactive_suggestion(suggestion["id"], 0, True, as_of=NOW)
    assert service.list_proactive_suggestions("pending")[0]["id"] == suggestion["id"]
    assert service.current_suggestion_strategy()["version"] == 1
    service.complete_task(task["id"])
    with pytest.raises(ValueError, match="stale_suggestion_evidence"):
        service.resolve_proactive_suggestion(suggestion["id"], 0, True, as_of=NOW)
    assert repo.list_memory_proposals() == []


def test_habit_and_review_signals_use_real_records_not_memory_write(flow_system):
    repo, _, _, _, _, service = flow_system
    habit = service.create_habit(HabitCreate(
        title="Exercise", timezone="Australia/Sydney", target_per_week=4,
    ))
    # No check-in means no observed trend.
    assert scan_suggestions(repo, NOW) == []
    local_day = NOW.astimezone(ZoneInfo("Australia/Sydney")).date()
    checkin = service.checkin_habit(habit["id"], HabitCheckin(
        local_date=local_day, completed=True,
    ))
    review = create_daily_review(repo, local_day, "Australia/Sydney", NOW)
    with repo.transaction() as connection:
        connection.execute(
            "UPDATE daily_reviews SET snapshot_json=?, note=?,revision=1 WHERE id=?",
            (json.dumps({"completed_tasks": 0, "unfinished_tasks": 3,
                         "active_habits": 1, "checked_habits": 1, "plan_conflicts": 0}),
             "Today was hard", review["id"]),
        )
    created = scan_suggestions(repo, NOW)
    by_kind = {item["kind"]: item for item in created}
    assert set(by_kind) == {"habit_gap", "daily_review"}
    assert habit["id"] in by_kind["habit_gap"]["source_ids"]
    assert checkin["id"] in by_kind["habit_gap"]["source_ids"]
    assert review["id"] in by_kind["daily_review"]["source_ids"]
    assert by_kind["daily_review"]["evidence"]["review"]["note_excerpt"] == "Today was hard"
    assert repo.list_approved_memories() == [] and repo.list_memory_proposals() == []


def test_worker_review_job_proactively_scans_once(flow_system):
    repo, _, _, _, _, _ = flow_system
    repo.create_task(TaskCreate(title="Overdue task", due_at=NOW - timedelta(days=4)))
    worker = Worker(repo, timezone_name="Australia/Sydney")
    with repo.transaction() as connection:
        connection.execute(
            "INSERT INTO scheduled_jobs(id,kind,dedupe_key,payload_json,due_at_utc,"
            "created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?)",
            ("review-job", "daily_review", "review:2026-10-09",
             json.dumps({"local_date": "2026-10-09", "timezone": "Australia/Sydney"}),
             NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
    result = worker.process_one(NOW)
    assert result is not None and result["id"] == "review-job"
    with repo.connection() as connection:
        assert connection.execute("SELECT status FROM scheduled_jobs WHERE id='review-job'").fetchone()[0] == "completed"
    assert len(repo.list_tasks()) == 1
    assert len(scan_suggestions(repo, NOW)) == 0
    assert len(list_suggestions(repo)) >= 1


def test_manual_review_scans_and_worker_scan_failure_preserves_review(flow_system, monkeypatch):
    repo, _, _, _, _, service = flow_system
    repo.create_task(TaskCreate(title="Overdue task", due_at=NOW - timedelta(days=4)))
    review = service.create_daily_review()
    assert review["id"]
    assert list_suggestions(repo)
    with repo.transaction() as connection:
        connection.execute(
            "INSERT INTO scheduled_jobs(id,kind,dedupe_key,payload_json,due_at_utc,"
            "created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?)",
            ("review-job-failure", "daily_review", "review:another-date",
             json.dumps({"local_date": "2026-10-08", "timezone": "Australia/Sydney"}),
             NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
    monkeypatch.setattr(worker_module, "scan_suggestions", lambda *_: (_ for _ in ()).throw(RuntimeError("scan failed")))
    Worker(repo).process_one(NOW)
    with repo.connection() as connection:
        assert connection.execute("SELECT status FROM scheduled_jobs WHERE id='review-job-failure'").fetchone()[0] == "completed"
        assert connection.execute("SELECT COUNT(*) FROM daily_reviews").fetchone()[0] >= 2
    assert any(event["event_type"] == "proactive_scan_failed" for event in repo.list_trace())


def test_v7_to_v8_backup_rollback_preserves_data(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite3"
    repo = Repository(path)
    repo.initialize()
    task = repo.create_task(TaskCreate(title="Keep me"))
    with sqlite3.connect(path) as connection:
        for table in ("proactive_suggestions", "suggestion_strategy_state",
                      "suggestion_strategy_versions", "external_audit",
                      "external_write_proposals", "mcp_operations", "mcp_servers"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=7")
    original = migrations.migrate_v8
    def fail_after_create(connection):
        original(connection)
        raise RuntimeError("injected_v8_failure")
    monkeypatch.setattr(migrations, "migrate_v8", fail_after_create)
    with pytest.raises(RuntimeError, match="injected_v8_failure"):
        repo.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master "
                                  "WHERE name='proactive_suggestions'").fetchone()[0] == 0
    assert list(tmp_path.glob("migration.backup-v7-*.sqlite3"))
    monkeypatch.setattr(migrations, "migrate_v8", original)
    repo.initialize()
    assert repo.get_task(task["id"])["title"] == "Keep me"
    with repo.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION
