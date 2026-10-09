"""The remaining P1 recurrence approval and scheduled-task replanning cases."""

import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.agents import MissingDeepSeekRunner
from personal_ai_os.config import load_config
from personal_ai_os.contracts import TaskCreate, TimeBlockCreate
from personal_ai_os.daily_planning import (
    approve_daily_plan, edit_daily_plan, list_daily_plans, propose_daily_plan,
)
from personal_ai_os.harness import Harness
from personal_ai_os.recurrence import generate_instances
from personal_ai_os.services import PersonalAIService
from personal_ai_os.storage import Repository
from personal_ai_os.tool_gateway import ToolGateway
from test_p1_daily_plan import DAY, instant, setup, task
from test_p1_life_plan import LifeRunner, MIXED, system


def test_three_weekdays_are_reviewed_then_confirmed_once(tmp_path):
    repository, _, _, service = system(tmp_path, LifeRunner())
    draft = service.start_plan(MIXED)
    suggestions = service.list_recurrence_suggestions(draft.run_id)
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert suggestion["weekdays"] == [0, 2, 4]
    assert suggestion["title"] == "运动" and not suggestion["confirmed"]
    assert repository.list_recurrence_rules() == [] and repository.list_tasks() == []
    with pytest.raises(ValueError, match="three_distinct_weekdays_required"):
        service.confirm_recurrence_suggestion(
            draft.run_id, draft.revision, suggestion["item_id"], [0, 2], DAY
        )
    edited = service.edit_plan(draft.run_id, draft.revision, draft.tasks)
    with pytest.raises(ValueError, match="stale_plan_revision"):
        service.confirm_recurrence_suggestion(
            draft.run_id, draft.revision, suggestion["item_id"], [0, 2, 4], DAY
        )
    rule = service.confirm_recurrence_suggestion(
        edited.run_id, edited.revision, suggestion["item_id"], [0, 2, 4], DAY
    )
    assert rule["frequency"] == "weekly_days" and json.loads(rule["weekdays_json"]) == [0, 2, 4]
    assert service.list_recurrence_suggestions(draft.run_id)[0]["confirmed"]
    assert service.confirm_recurrence_suggestion(
        edited.run_id, edited.revision, suggestion["item_id"], [0, 2, 4], DAY
    )["id"] == rule["id"]
    with pytest.raises(ValueError, match="already_confirmed"):
        service.confirm_recurrence_suggestion(
            edited.run_id, edited.revision, suggestion["item_id"], [1, 3, 5], DAY
        )
    reopened = Repository(repository.path)
    reopened.initialize()
    assert reopened.get_recurrence_rule(rule["id"])["title"] == "运动"
    created = generate_instances(reopened, rule["id"], DAY, DAY + timedelta(days=6),
                                 as_of_local_date=DAY)
    assert len(created) == 3
    assert generate_instances(reopened, rule["id"], DAY, DAY + timedelta(days=6),
                              as_of_local_date=DAY) == []


def test_overdue_scheduled_task_proposes_move_without_early_write(tmp_path):
    repository = setup(tmp_path)
    yesterday = instant(9) - timedelta(days=1)
    task_id = task(repository, "逾期学习", start=yesterday, end=yesterday + timedelta(hours=1))
    draft = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8))
    assert len(draft.actions) == 1 and not draft.conflicts
    action = draft.actions[0]
    assert action.kind == "reschedule_task" and action.old_start_at == yesterday
    assert action.new_start_at == instant(9) and "逾期" in action.reason
    assert repository.get_task(task_id)["start_at_utc"] == yesterday.isoformat()
    edited = edit_daily_plan(repository, draft.id, draft.revision, [action.model_copy(update={
        "new_start_at": instant(10), "new_end_at": instant(11),
    })])
    with pytest.raises(ValueError, match="stale_daily_revision"):
        approve_daily_plan(repository, draft.id, draft.revision)
    result = approve_daily_plan(repository, draft.id, edited.revision)
    assert result[0]["start_at_utc"] == instant(10).isoformat()
    assert approve_daily_plan(repository, draft.id, edited.revision) == result


def test_availability_change_proposes_move_and_rechecks_baseline(tmp_path):
    repository = setup(tmp_path)
    task_id = task(repository, "已排运动", "life", start=instant(9), end=instant(10))
    block = repository.list_time_blocks()[0]
    repository.update_time_block(block["id"], TimeBlockCreate(
        kind="available", start_at=instant(11), end_at=instant(13)
    ), allow_pending_replan=True)
    assert repository.get_task(task_id)["start_at_utc"] == instant(9).isoformat()
    draft = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8))
    assert len(draft.actions) == 1 and not draft.conflicts
    action = draft.actions[0]
    assert action.old_start_at == instant(9) and action.new_start_at == instant(11)
    assert "原安排" in action.reason and action.source == f"task:{task_id}"
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=instant(14), end_at=instant(15)
    ))
    with pytest.raises(ValueError, match="stale_plan"):
        approve_daily_plan(repository, draft.id, draft.revision)
    assert repository.get_task(task_id)["start_at_utc"] == instant(9).isoformat()
    fresh = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8))
    assert approve_daily_plan(repository, fresh.id, fresh.revision)[0]["start_at_utc"] == instant(11).isoformat()


def test_no_legal_new_slot_keeps_old_task_and_shows_conflict(tmp_path):
    repository = setup(tmp_path)
    task_id = task(repository, "旧安排", start=instant(9), end=instant(10))
    repository.delete_time_block(repository.list_time_blocks()[0]["id"],
                                 allow_pending_replan=True)
    draft = propose_daily_plan(repository, DAY, "UTC", as_of=instant(8))
    assert len(draft.actions) == 1
    assert draft.actions[0].old_start_at == instant(9)
    assert draft.actions[0].new_start_at is None
    assert draft.actions[0].unassigned_reason == "no_availability"
    assert draft.conflicts
    with pytest.raises(ValueError, match="daily_plan_conflict"):
        approve_daily_plan(repository, draft.id, draft.revision)
    assert repository.get_task(task_id)["start_at_utc"] == instant(9).isoformat()


def test_user_completion_triggers_reviewable_daily_draft(tmp_path):
    zone = ZoneInfo("Australia/Sydney")
    day = datetime.now(zone).date() + timedelta(days=1)
    repository = Repository(tmp_path / "completion.sqlite3")
    repository.initialize()
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=datetime.combine(day, time(9), zone),
        end_at=datetime.combine(day, time(12), zone),
    ))
    completed = repository.create_task(TaskCreate(
        title="已完成", start_at=datetime.combine(day, time(9), zone),
        end_at=datetime.combine(day, time(10), zone),
    ))
    remaining = repository.create_task(TaskCreate(title="待安排", estimated_minutes=60))
    config = load_config(environ={})
    registry = AgentRegistry(repository)
    gateway = ToolGateway(repository, registry, config.timezone)
    service = PersonalAIService(
        repository, gateway,
        Harness(repository, registry, gateway, MissingDeepSeekRunner(), config),
    )
    service.complete_task(completed["id"])
    drafts = list_daily_plans(repository)
    assert len(drafts) == 1 and drafts[0].status == "draft"
    assert drafts[0].actions[0].task_id == remaining["id"]
    assert repository.get_task(remaining["id"])["start_at_utc"] is None


def test_new_availability_triggers_draft_for_unscheduled_task(tmp_path):
    repository, _, _, service = system(tmp_path, LifeRunner())
    task_row = service.create_task(TaskCreate(title="待安排运动", estimated_minutes=60))
    zone = ZoneInfo("Australia/Sydney")
    day = datetime.now(zone).date() + timedelta(days=4)
    service.create_time_block(TimeBlockCreate(
        kind="available", start_at=datetime.combine(day, time(9), zone),
        end_at=datetime.combine(day, time(11), zone),
    ))
    drafts = [item for item in list_daily_plans(repository) if item.local_date == day]
    assert len(drafts) == 1 and drafts[0].actions[0].task_id == task_row["id"]
    assert repository.get_task(task_row["id"])["start_at_utc"] is None


def test_valid_overnight_schedule_is_not_moved_from_partial_day(tmp_path):
    repository = Repository(tmp_path / "overnight.sqlite3")
    repository.initialize()
    start = datetime(2026, 10, 11, 22, tzinfo=timezone.utc)
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=start, end_at=start + timedelta(hours=4)
    ))
    repository.create_task(TaskCreate(
        title="跨午夜任务", start_at=start + timedelta(hours=1),
        end_at=start + timedelta(hours=3), estimated_minutes=120,
    ))
    draft = propose_daily_plan(
        repository, DAY, "UTC", as_of=datetime(2026, 10, 11, 20, tzinfo=timezone.utc)
    )
    assert draft.actions == []
