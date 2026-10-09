"""The existing today page is the only UI path for daily draft operations."""

from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

from personal_ai_os.contracts import HabitCheckin, HabitCreate, RecurrenceRuleCreate, TaskCreate, TimeBlockCreate
from personal_ai_os.daily_planning import list_daily_plans
from personal_ai_os.recurrence import generate_instances
from personal_ai_os.storage import Repository


APP = str(Path(__file__).resolve().parents[1] / "app.py")
ZONE = ZoneInfo("Australia/Sydney")


def widget(items, label):
    return next(item for item in items if item.label == label)


def setup(tmp_path, monkeypatch):
    path = tmp_path / "daily_ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    day = datetime.now(ZONE).date() + timedelta(days=1)
    repository = Repository(path)
    repository.initialize()
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=datetime.combine(day, time(9), ZONE),
        end_at=datetime.combine(day, time(12), ZONE),
    ))
    at = AppTest.from_file(APP).run(timeout=30)
    at.date_input[0].set_value(day).run(timeout=30)
    return repository, at, day


def test_today_page_shows_both_domains_recurrence_habit_and_real_reason(tmp_path, monkeypatch):
    repository, at, day = setup(tmp_path, monkeypatch)
    study = repository.create_task(TaskCreate(title="学习待办"))
    rule = repository.create_recurrence_rule(RecurrenceRuleCreate(
        title="运动待办", domain="life", frequency="daily",
        timezone="Australia/Sydney", start_date=day,
    ))
    generated = generate_instances(repository, rule["id"], day, day, as_of_local_date=day)
    habit = repository.create_habit(HabitCreate(
        title="阅读习惯", timezone="Australia/Sydney", target_per_week=3
    ))
    repository.checkin_habit(habit["id"], HabitCheckin(local_date=day, completed=True))
    at.run(timeout=30)
    assert not at.exception
    frames = [item.value for item in at.dataframe]
    rendered = "\n".join(frame.to_string() for frame in frames)
    assert "学习待办" in rendered and "运动待办" in rendered
    assert "no_preferred_time" in rendered
    assert "阅读习惯" in rendered and "1/3" in rendered
    assert repository.get_task(study["id"])["start_at_utc"] is None
    assert repository.get_task(generated[0]["task_id"])["start_at_utc"] is None


def test_page_edits_daily_draft_then_confirms_without_early_task_change(tmp_path, monkeypatch):
    repository, at, day = setup(tmp_path, monkeypatch)
    task = repository.create_task(TaskCreate(title="明日复习", estimated_minutes=60))
    at.run(timeout=30)
    widget(at.button, "生成每日草案").click().run(timeout=30)
    assert not at.exception
    draft = list_daily_plans(repository)[0]
    assert draft.status == "draft" and draft.revision == 0
    assert repository.get_task(task["id"])["start_at_utc"] is None
    start = datetime.combine(day, time(10), ZONE).isoformat()
    end = datetime.combine(day, time(11), ZONE).isoformat()
    widget(at.text_input, "每日新开始 1（ISO 8601，可留空）").set_value(start)
    widget(at.text_input, "每日新结束 1（ISO 8601，可留空）").set_value(end)
    widget(at.button, "保存每日草案修改").click().run(timeout=30)
    assert not at.exception
    draft = list_daily_plans(repository)[0]
    assert draft.revision == 1 and draft.status == "draft"
    assert repository.get_task(task["id"])["start_at_utc"] is None
    widget(at.button, "确认每日计划").click().run(timeout=30)
    assert not at.exception
    assert list_daily_plans(repository)[0].status == "committed"
    assert repository.get_task(task["id"])["start_at_utc"] == datetime.combine(
        day, time(10), ZONE
    ).astimezone(ZoneInfo("UTC")).isoformat()


def test_page_can_add_create_and_reschedule_actions(tmp_path, monkeypatch):
    repository, at, day = setup(tmp_path, monkeypatch)
    existing = repository.create_task(TaskCreate(
        title="已排运动", estimated_minutes=60,
        start_at=datetime.combine(day, time(9), ZONE),
        end_at=datetime.combine(day, time(10), ZONE),
    ))
    at.run(timeout=30)
    widget(at.button, "生成每日草案").click().run(timeout=30)
    assert list_daily_plans(repository)[0].actions == []
    widget(at.text_input, "每日新增任务标题").set_value("新学习事项")
    widget(at.text_input, "每日新增开始（ISO 8601）").set_value(
        datetime.combine(day, time(10), ZONE).isoformat()
    )
    widget(at.text_input, "每日新增结束（ISO 8601）").set_value(
        datetime.combine(day, time(11), ZONE).isoformat()
    )
    widget(at.button, "添加新增任务动作").click().run(timeout=30)
    draft = list_daily_plans(repository)[0]
    assert draft.revision == 1 and draft.actions[0].kind == "create_task"
    assert len(repository.list_tasks()) == 1
    # Move the existing task to 11:00 while the new item remains at 10:00.
    widget(at.text_input, "重排新开始（ISO 8601）").set_value(
        datetime.combine(day, time(11), ZONE).isoformat()
    )
    widget(at.text_input, "重排新结束（ISO 8601）").set_value(
        datetime.combine(day, time(12), ZONE).isoformat()
    )
    widget(at.button, "添加重排动作").click().run(timeout=30)
    draft = list_daily_plans(repository)[0]
    assert draft.revision == 2, [item.value for item in at.error]
    assert {item.kind for item in draft.actions} == {
        "create_task", "reschedule_task"
    }
    assert repository.get_task(existing["id"])["start_at_utc"] == datetime.combine(
        day, time(9), ZONE
    ).astimezone(ZoneInfo("UTC")).isoformat()
    widget(at.button, "确认每日计划").click().run(timeout=30)
    assert not at.exception
    assert len(repository.list_tasks()) == 2
    assert {row["title"] for row in repository.list_tasks()} == {"已排运动", "新学习事项"}
