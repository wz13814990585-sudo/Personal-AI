"""Durable jobs, lease recovery, quiet hours and notification outcomes."""

import sqlite3
import subprocess
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from personal_ai_os import migrations
from personal_ai_os.contracts import RecurrenceRuleCreate, TaskCreate, TimeBlockCreate
from personal_ai_os.daily_planning import list_daily_plans
from personal_ai_os.storage import Repository, SCHEMA
from personal_ai_os.worker import (
    Worker, create_daily_review, get_task_reminder, list_daily_reviews,
    list_notifications, quiet_end, save_daily_review_note, set_task_reminder,
)


UTC = timezone.utc
SYDNEY = ZoneInfo("Australia/Sydney")


class FakeNotifier:
    def __init__(self, *, supported=True, failure=None):
        self.supported_value = supported
        self.failure = failure
        self.calls = []

    def supported(self):
        return self.supported_value

    def send(self, title, body):
        self.calls.append((title, body))
        if self.failure:
            raise self.failure


def repository(tmp_path):
    result = Repository(tmp_path / "worker.sqlite3")
    result.initialize()
    return result


def scheduled_task(repository, start):
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=start, end_at=start + timedelta(minutes=30),
    ))
    task = repository.create_task(TaskCreate(
        title="学习提醒", estimated_minutes=30, start_at=start,
        end_at=start + timedelta(minutes=30),
    ))
    set_task_reminder(repository, task["id"], 10)
    return task


def test_recurrence_lease_expiry_recovery_and_deduplication(tmp_path):
    repo = repository(tmp_path)
    now = datetime(2026, 10, 9, 1, tzinfo=UTC)
    day = now.astimezone(SYDNEY).date()
    rule = repo.create_recurrence_rule(RecurrenceRuleCreate(
        title="喝水", domain="life", frequency="daily", timezone="Australia/Sydney", start_date=day,
    ))
    first = Worker(repo, owner="first", lease_seconds=2)
    first.seed(now)
    claimed = first.claim(now)
    assert claimed["kind"] == "recurrence_scan"
    assert not repo.list_recurrence_instances(rule["id"])
    second = Worker(Repository(repo.path), owner="second", lease_seconds=2)
    assert second.process_one(now + timedelta(seconds=3)) is not None
    assert len(repo.list_recurrence_instances(rule["id"])) == 1
    assert second.run_once(now + timedelta(seconds=4)) >= 1
    assert second.run_once(now + timedelta(seconds=5)) == 0
    assert len(repo.list_recurrence_instances(rule["id"])) == 1
    assert any(action.title == "喝水" for draft in list_daily_plans(repo) for action in draft.actions)
    with repo.connection() as connection:
        job = connection.execute("SELECT status,attempts FROM scheduled_jobs WHERE id=?", (claimed["id"],)).fetchone()
    assert tuple(job) == ("completed", 2)


def test_reminder_restart_dedupe_quiet_and_missed(tmp_path):
    repo = repository(tmp_path)
    repo.set_setting("timezone", "Australia/Sydney")
    repo.set_setting("quiet_start", "22:00")
    repo.set_setting("quiet_end", "07:00")
    due_local = datetime(2026, 10, 9, 23, 30, tzinfo=SYDNEY)
    task = scheduled_task(repo, due_local.astimezone(UTC) + timedelta(minutes=510))
    set_task_reminder(repo, task["id"], 510)
    original = due_local.astimezone(UTC)
    due = quiet_end(original, SYDNEY, "22:00", "07:00")
    assert due.astimezone(SYDNEY).strftime("%Y-%m-%d %H:%M") == "2026-10-10 07:00"
    notifier = FakeNotifier()
    before = Worker(repo, notifier=notifier)
    assert before.run_once(original) >= 1  # recurrence/review jobs may also be due
    assert list_notifications(repo) == []
    restarted = Repository(repo.path)
    restarted.initialize()
    Worker(restarted, notifier=notifier).run_once(due)
    notices = list_notifications(restarted)
    assert len(notices) == 1 and notices[0]["status"] == "delivered"
    assert notices[0]["original_due_at_utc"] == original.isoformat()
    assert notices[0]["system_status"] == "disabled" and not notifier.calls
    Worker(restarted, notifier=notifier).run_once(due + timedelta(seconds=1))
    assert len(list_notifications(restarted)) == 1

    overnight_start = datetime(2026, 10, 10, 1, 0, tzinfo=SYDNEY).astimezone(UTC)
    overnight = scheduled_task(restarted, overnight_start)
    Worker(restarted).run_once(due + timedelta(seconds=2))
    overnight_notice = next(item for item in list_notifications(restarted)
                            if item["task_id"] == overnight["id"])
    assert overnight_notice["status"] == "missed"
    late = scheduled_task(restarted, due + timedelta(minutes=120))
    # A past-due reminder is retained in-app as missed after downtime.
    Worker(restarted).run_once(due + timedelta(minutes=135))
    missed = next(item for item in list_notifications(restarted) if item["task_id"] == late["id"])
    assert missed["status"] == "missed" and missed["system_status"] == "missed"


def test_opt_in_system_result_failure_unknown_unsupported_and_crash_boundary(tmp_path):
    repo = repository(tmp_path)
    repo.set_setting("timezone", "UTC")
    repo.set_setting("quiet_start", "00:00")
    repo.set_setting("quiet_end", "00:00")
    repo.set_setting("system_notifications_enabled", True)
    now = datetime(2026, 10, 9, 12, tzinfo=UTC)
    task = scheduled_task(repo, now + timedelta(minutes=10))
    success = FakeNotifier()
    Worker(repo, notifier=success).run_once(now)
    assert len(success.calls) == 1
    assert list_notifications(repo)[0]["system_status"] == "delivered"
    Worker(repo, notifier=success).run_once(now + timedelta(seconds=1))
    assert len(success.calls) == 1

    second = scheduled_task(repo, now + timedelta(hours=1, minutes=10))
    failure = FakeNotifier(failure=RuntimeError("permission denied"))
    Worker(repo, notifier=failure).run_once(now + timedelta(hours=1))
    notice = next(row for row in list_notifications(repo) if row["task_id"] == second["id"])
    assert notice["system_status"] == "failed" and "permission denied" in notice["error"]

    third = scheduled_task(repo, now + timedelta(hours=2, minutes=10))
    uncertain = FakeNotifier(failure=subprocess.TimeoutExpired("notifier", 10))
    Worker(repo, notifier=uncertain).run_once(now + timedelta(hours=2))
    notice = next(row for row in list_notifications(repo) if row["task_id"] == third["id"])
    assert notice["system_status"] == "unknown"
    Worker(repo, notifier=uncertain).run_once(now + timedelta(hours=2, seconds=1))
    assert len(uncertain.calls) == 1

    fourth = scheduled_task(repo, now + timedelta(hours=3, minutes=10))
    Worker(repo, notifier=FakeNotifier(supported=False)).run_once(now + timedelta(hours=3))
    notice = next(row for row in list_notifications(repo) if row["task_id"] == fourth["id"])
    assert notice["system_status"] == "unsupported"

    # Simulate a crash after the durable unknown marker was written but before delivery.
    fifth = scheduled_task(repo, now + timedelta(hours=4, minutes=10))
    worker = Worker(repo, notifier=FakeNotifier(), owner="crashed", lease_seconds=1)
    worker.seed(now + timedelta(hours=4))
    with repo.transaction() as connection:
        job = connection.execute("SELECT * FROM scheduled_jobs WHERE dedupe_key LIKE ?",
                                 (f"reminder:{fifth['id']}:%",)).fetchone()
        connection.execute("UPDATE scheduled_jobs SET status='leased',lease_owner='crashed',lease_until_utc=? WHERE id=?",
                           ((now + timedelta(hours=4, seconds=1)).isoformat(), job["id"]))
        connection.execute("INSERT INTO notifications(id,job_id,task_id,title,body,original_due_at_utc,"
                           "visible_at_utc,status,system_status) VALUES (?,?,?,?,?,?,?,?,?)",
                           ("crash-notice", job["id"], fifth["id"], "学习提醒", "body", job["due_at_utc"],
                            job["due_at_utc"], "delivered", "unknown"))
    after_crash = FakeNotifier()
    Worker(repo, notifier=after_crash).run_once(now + timedelta(hours=4, seconds=2))
    assert after_crash.calls == []
    assert len([row for row in list_notifications(repo) if row["task_id"] == fifth["id"]]) == 1


def test_review_note_is_editable_persistent_and_never_creates_memory(tmp_path):
    repo = repository(tmp_path)
    now = datetime(2026, 10, 9, 12, tzinfo=UTC)
    Worker(repo).run_once(now)
    review = list_daily_reviews(repo)[0]
    assert review["local_date"] == "2026-10-09"
    assert len(list_daily_plans(repo)) == 1
    Worker(repo).run_once(now + timedelta(seconds=1))
    assert len(list_daily_plans(repo)) == 1
    saved = save_daily_review_note(repo, review["id"], 0, "今天有点累")
    assert saved["revision"] == 1
    with pytest.raises(ValueError, match="stale_review_revision"):
        save_daily_review_note(repo, review["id"], 0, "覆盖")
    reopened = Repository(repo.path)
    reopened.initialize()
    assert list_daily_reviews(reopened)[0]["note"] == "今天有点累"
    assert create_daily_review(reopened, date(2026, 10, 9), "Australia/Sydney", now)["id"] == review["id"]
    assert reopened.list_approved_memories() == []
    assert reopened.list_memory_proposals() == []


def test_v3_to_v4_migration_failure_keeps_data_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "worker.sqlite3"
    # Build an actual v3 file, without any v4/v5 columns or tables.
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        migrations.migrate_v1(connection)
        migrations.migrate_v2(connection)
        migrations.migrate_v3(connection)
        connection.execute("PRAGMA user_version=3")
        connection.commit()
    repo = Repository(path)
    original = migrations.migrate_v4
    def injected(connection):
        original(connection)
        raise RuntimeError("injected v4 failure")
    monkeypatch.setattr(migrations, "migrate_v4", injected)
    with pytest.raises(RuntimeError, match="injected v4 failure"):
        repo.initialize()
    with sqlite3.connect(repo.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='scheduled_jobs'").fetchone() is None
    monkeypatch.setattr(migrations, "migrate_v4", original)
    repo.initialize()
    assert list(tmp_path.glob("worker.backup-v3-*.sqlite3"))
    with repo.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION
