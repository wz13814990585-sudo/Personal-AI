"""Daily drafts never mutate tasks until a fresh, atomic user approval."""

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from personal_ai_os import daily_planning, migrations
from personal_ai_os.contracts import (
    DailyPlanAction, HabitCheckin, HabitCreate, RecurrenceRuleCreate, TimeBlockCreate,
)
from personal_ai_os.daily_planning import (
    approve_daily_plan, daily_overview, edit_daily_plan, get_daily_plan, propose_daily_plan,
)
from personal_ai_os.recurrence import generate_instances
from personal_ai_os.storage import Repository, SCHEMA


DAY = date(2026, 10, 12)


def instant(hour, minute=0):
    return datetime(2026, 10, 12, hour, minute, tzinfo=timezone.utc)


def setup(tmp_path, *, available=True):
    repository = Repository(tmp_path / "daily.sqlite3")
    repository.initialize()
    if available:
        repository.create_time_block(TimeBlockCreate(
            kind="available", start_at=instant(9), end_at=instant(12)
        ))
    return repository


def task(repository, title, domain="study", minutes=60, start=None, end=None):
    task_id = uuid4().hex
    with repository.transaction() as connection:
        connection.execute(
            "INSERT INTO tasks(id,title,status,priority,estimated_minutes,start_at_utc,end_at_utc,domain) "
            "VALUES (?,?,'pending','medium',?,?,?,?)",
            (task_id, title, minutes, start.isoformat() if start else None,
             end.isoformat() if end else None, domain),
        )
    return task_id


def test_daily_plan_reorders_unscheduled_study_life_without_early_write(tmp_path):
    repository = setup(tmp_path)
    study = task(repository, "复习", "study")
    life = task(repository, "运动", "life")
    before = {item["id"]: item for item in repository.list_tasks()}
    draft = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8), trigger_key="morning")
    same = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8), trigger_key="morning")
    assert draft.id == same.id and draft.revision == 0
    assert len(draft.actions) == 2 and not draft.conflicts
    assert all(item.kind == "reschedule_task" for item in draft.actions)
    assert {item.domain for item in draft.actions} == {"study", "life"}
    assert all(item.source == f"task:{item.task_id}" for item in draft.actions)
    assert all(item.old_start_at is None and item.new_start_at for item in draft.actions)
    assert {item["id"]: item for item in repository.list_tasks()} == before
    assert not (draft.actions[0].new_start_at < draft.actions[1].new_end_at
                and draft.actions[1].new_start_at < draft.actions[0].new_end_at)

    edited = edit_daily_plan(repository, draft.id, 0, draft.actions)
    assert edited.revision == 1
    with pytest.raises(ValueError, match="stale_daily_revision"):
        approve_daily_plan(repository, draft.id, 0)
    result = approve_daily_plan(repository, draft.id, 1)
    assert {row["id"] for row in result} == {study, life}
    assert all(row["start_at_utc"] and row["version"] == 2 for row in result)
    assert approve_daily_plan(repository, draft.id, 1) == result
    reopened = Repository(repository.path)
    reopened.initialize()
    assert get_daily_plan(reopened, draft.id).status == "committed"
    assert reopened.get_task(study)["start_at_utc"]
    with pytest.raises(ValueError, match="stale_daily_revision"):
        approve_daily_plan(repository, draft.id, 0)


def test_unavailable_and_conflicting_actions_are_explained_and_not_applied(tmp_path):
    repository = setup(tmp_path, available=False)
    task_id = task(repository, "无时段任务")
    draft = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8))
    assert draft.actions[0].unassigned_reason == "no_availability"
    assert "no_availability" in draft.conflicts[0]
    with pytest.raises(ValueError, match="daily_plan_conflict"):
        approve_daily_plan(repository, draft.id, 0)
    assert repository.get_task(task_id)["start_at_utc"] is None
    assert get_daily_plan(repository, draft.id).status == "draft"

    other = setup(tmp_path / "other")
    first = task(other, "first", "study")
    second = task(other, "second", "life")
    plan = propose_daily_plan(other, DAY, "UTC", as_of=instant(8))
    bad = [item.model_copy(update={"new_start_at": instant(9), "new_end_at": instant(10)})
           for item in plan.actions]
    edited = edit_daily_plan(other, plan.id, 0, bad)
    assert any("task_overlap" in issue for issue in edited.conflicts)
    with pytest.raises(ValueError, match="task_overlap"):
        approve_daily_plan(other, edited.id, edited.revision)
    assert other.get_task(first)["start_at_utc"] is None
    assert other.get_task(second)["start_at_utc"] is None


@pytest.mark.parametrize("change", ["task", "time", "rule", "memory"])
def test_changed_baseline_rejects_approval_without_partial_write(tmp_path, change):
    repository = setup(tmp_path)
    task_id = task(repository, "待安排")
    draft = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8))
    if change == "task":
        repository.set_task_status(task_id, "in_progress")
    elif change == "time":
        repository.create_time_block(TimeBlockCreate(
            kind="busy", start_at=instant(11), end_at=instant(12)
        ))
    elif change == "rule":
        repository.create_recurrence_rule(RecurrenceRuleCreate(
            title="new rule", frequency="daily", timezone="UTC", start_date=DAY
        ))
    else:
        with repository.transaction() as connection:
            connection.execute(
                "INSERT INTO memories(id,kind,value_json,status,created_at_utc,updated_at_utc) "
                "VALUES ('new-memory','study_time_avoid','{\"start\":\"09:00\",\"end\":\"10:00\"}',"
                "'approved','x','x')"
            )
    with pytest.raises(ValueError, match="stale_plan"):
        approve_daily_plan(repository, draft.id, draft.revision)
    assert repository.get_task(task_id)["start_at_utc"] is None
    assert get_daily_plan(repository, draft.id).status == "draft"


def test_create_and_reschedule_are_atomic_with_failure_injection(tmp_path, monkeypatch):
    repository = setup(tmp_path)
    task_id = task(repository, "既有任务")
    draft = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8))
    new = DailyPlanAction(
        action_id="new-item", kind="create_task", title="新生活事项", domain="life",
        priority="high", estimated_minutes=60, new_start_at=instant(10),
        new_end_at=instant(11), source="user_daily_plan", reason="用户添加",
    )
    edited = edit_daily_plan(repository, draft.id, 0, [*draft.actions, new])
    assert edited.conflicts == [] and len(repository.list_tasks()) == 1
    original_apply = daily_planning._apply_action
    calls = 0

    def fail_second(connection, action):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second action failure")
        return original_apply(connection, action)

    monkeypatch.setattr(daily_planning, "_apply_action", fail_second)
    with pytest.raises(RuntimeError, match="injected second action failure"):
        approve_daily_plan(repository, draft.id, edited.revision)
    assert len(repository.list_tasks()) == 1
    assert repository.get_task(task_id)["start_at_utc"] is None
    assert get_daily_plan(repository, draft.id).status == "draft"
    monkeypatch.setattr(daily_planning, "_apply_action", original_apply)
    result = approve_daily_plan(repository, draft.id, edited.revision)
    assert len(result) == 2 and {row["domain"] for row in result} == {"study", "life"}
    assert len(repository.list_tasks()) == 2


def test_rearrange_existing_scheduled_task_and_reject_source_tampering(tmp_path):
    repository = setup(tmp_path)
    task_id = task(repository, "已排", "life", start=instant(9), end=instant(10))
    draft = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8))
    assert not draft.actions
    current = repository.get_task(task_id)
    action = DailyPlanAction(
        action_id="move", kind="reschedule_task", task_id=task_id,
        title=current["title"], domain="life", priority=current["priority"],
        estimated_minutes=current["estimated_minutes"],
        old_start_at=instant(9), old_end_at=instant(10), old_version=current["version"],
        new_start_at=instant(10), new_end_at=instant(11),
        source=f"task:{task_id}", reason="用户调整",
    )
    with pytest.raises(ValueError, match="daily_task_source_changed"):
        edit_daily_plan(repository, draft.id, 0, [action.model_copy(update={"domain": "study"})])
    edited = edit_daily_plan(repository, draft.id, 0, [action])
    assert edited.actions[0].old_start_at == instant(9)
    assert repository.get_task(task_id)["start_at_utc"] == instant(9).isoformat()
    moved = approve_daily_plan(repository, draft.id, edited.revision)
    assert moved[0]["start_at_utc"] == instant(10).isoformat()


def test_daily_create_rejects_non_user_source(tmp_path):
    repository = setup(tmp_path)
    draft = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8))
    forged = DailyPlanAction(
        action_id="forged", kind="create_task", title="not approved by user",
        domain="life", priority="medium", estimated_minutes=30,
        new_start_at=instant(9), new_end_at=instant(9, 30),
        source="agent:life", reason="forged",
    )
    with pytest.raises(PermissionError, match="daily_create_requires_user_source"):
        edit_daily_plan(repository, draft.id, draft.revision, [forged])
    assert repository.list_tasks() == []
    assert get_daily_plan(repository, draft.id).revision == 0


def test_overview_includes_recurrence_habits_and_unscheduled_reason(tmp_path):
    repository = setup(tmp_path)
    study_id = task(repository, "复习", "study", start=instant(9), end=instant(10))
    rule = repository.create_recurrence_rule(RecurrenceRuleCreate(
        title="运动", domain="life", frequency="daily", timezone="UTC", start_date=DAY,
    ))
    generated = generate_instances(repository, rule["id"], DAY, DAY, as_of_local_date=DAY)
    habit = repository.create_habit(HabitCreate(title="阅读", timezone="UTC", target_per_week=3))
    repository.checkin_habit(habit["id"], HabitCheckin(local_date=DAY, completed=True))
    view = daily_overview(repository, DAY, "UTC")
    assert {item["id"] for item in view["tasks"]} == {study_id, generated[0]["task_id"]}
    recurring = next(item for item in view["tasks"] if item["recurrence_rule_id"])
    assert recurring["unassigned_reason"] == "no_preferred_time"
    assert recurring["recurrence_local_date"] == DAY.isoformat()
    assert view["habits"][0]["completed_this_week"] == 1
    assert view["habits"][0]["checked_today"] is True
    assert len(view["time_blocks"]) == 1


def test_future_recurrence_instances_do_not_enter_earlier_daily_plan(tmp_path):
    repository = setup(tmp_path)
    rule = repository.create_recurrence_rule(RecurrenceRuleCreate(
        title="daily", domain="life", frequency="daily", timezone="UTC", start_date=DAY,
    ))
    generated = generate_instances(repository, rule["id"], DAY, DAY + timedelta(days=2),
                                   as_of_local_date=DAY)
    assert len(generated) == 3
    overview = daily_overview(repository, DAY, "UTC", as_of=instant(8))
    assert {item["id"] for item in overview["tasks"]} == {generated[0]["task_id"]}
    draft = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8))
    assert [item.task_id for item in draft.actions] == [generated[0]["task_id"]]


def test_habit_progress_uses_its_own_local_date(tmp_path):
    repository = setup(tmp_path)
    habit = repository.create_habit(HabitCreate(
        title="Sydney habit", timezone="Australia/Sydney", target_per_week=3
    ))
    repository.checkin_habit(habit["id"], HabitCheckin(local_date=DAY, completed=True))
    view = daily_overview(
        repository, date(2026, 10, 11), "UTC",
        as_of=datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc),
    )
    assert view["habits"][0]["habit_local_date"] == DAY.isoformat()
    assert view["habits"][0]["checked_today"] is True


def test_overview_shows_overnight_task_that_overlaps_selected_day(tmp_path):
    repository = setup(tmp_path)
    overnight = task(
        repository, "overnight", "life",
        start=datetime(2026, 10, 11, 23, tzinfo=timezone.utc),
        end=datetime(2026, 10, 12, 1, tzinfo=timezone.utc),
    )
    assert overnight in {item["id"] for item in daily_overview(repository, DAY, "UTC")["tasks"]}


def test_daily_slot_search_uses_real_instants_across_dst_jump(tmp_path):
    zone = ZoneInfo("Australia/Sydney")
    day = date(2026, 10, 4)
    repository = Repository(tmp_path / "dst.sqlite3")
    repository.initialize()
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=datetime.combine(day, time(0), zone),
        end_at=datetime.combine(day, time(5), zone),
    ))
    task(repository, "occupied", start=datetime.combine(day, time(0), zone),
         end=datetime.combine(day, time(1, 30), zone))
    wanted = task(repository, "after jump", minutes=60)
    draft = propose_daily_plan(
        repository, day, "Australia/Sydney",
        as_of=datetime(2026, 10, 3, 0, tzinfo=timezone.utc),
    )
    action = next(item for item in draft.actions if item.task_id == wanted)
    assert action.new_start_at.astimezone(zone).hour == 1
    assert action.new_end_at.astimezone(zone).hour == 3
    assert (action.new_end_at.astimezone(timezone.utc)
            - action.new_start_at.astimezone(timezone.utc)).total_seconds() == 3600


def test_v2_to_v3_migration_backup_and_failure_rollback(tmp_path, monkeypatch):
    path = tmp_path / "old_v2.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        migrations.migrate_v1(connection)
        migrations.migrate_v2(connection)
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    repository = Repository(path)
    task_id = task(repository, "旧任务")
    original = migrations.migrate_v3

    def fail_after_ddl(connection):
        original(connection)
        raise RuntimeError("injected v3 failure")

    monkeypatch.setattr(migrations, "migrate_v3", fail_after_ddl)
    with pytest.raises(RuntimeError, match="injected v3 failure"):
        repository.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "旧任务"
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='daily_plan_proposals'"
        ).fetchone() is None
    monkeypatch.setattr(migrations, "migrate_v3", original)
    repository.initialize()
    assert repository.get_task(task_id)["title"] == "旧任务"
    assert len(list(tmp_path.glob("old_v2.backup-v2-*.sqlite3"))) >= 1
    with repository.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION
