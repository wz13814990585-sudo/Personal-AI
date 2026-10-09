from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from personal_ai_os.contracts import HabitCheckin, HabitCreate
from personal_ai_os.storage import Repository


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


def test_habit_checkin_correction_and_reopen(tmp_path):
    path = tmp_path / "habit.sqlite3"
    repository = Repository(path)
    repository.initialize()
    habit = repository.create_habit(HabitCreate(
        title="exercise", timezone="Australia/Sydney", target_per_week=3
    ))
    first = repository.checkin_habit(habit["id"], HabitCheckin(
        local_date=date(2026, 10, 8), completed=True, note="done"
    ))
    corrected = repository.checkin_habit(habit["id"], HabitCheckin(
        local_date=date(2026, 10, 8), completed=False, note="corrected"
    ))
    assert corrected["id"] == first["id"]
    assert corrected["completed"] == 0
    reopened = Repository(path)
    reopened.initialize()
    assert reopened.list_habit_checkins(habit["id"])[0]["note"] == "corrected"
    reopened.set_habit_status(habit["id"], "paused")
    try:
        reopened.checkin_habit(habit["id"], HabitCheckin(
            local_date=date(2026, 10, 9), completed=True
        ))
        assert False, "paused habit unexpectedly accepted a checkin"
    except ValueError as exc:
        assert str(exc) == "habit_paused"
    reopened.set_habit_status(habit["id"], "active")
    assert reopened.get_habit(habit["id"])["status"] == "active"


def test_habit_correction_records_user_audit(flow_system):
    repository, _, _, _, _, service = flow_system
    habit = service.create_habit(HabitCreate(title="reading", timezone="Australia/Sydney"))
    service.checkin_habit(habit["id"], HabitCheckin(local_date=date(2026, 10, 8), completed=True))
    service.checkin_habit(habit["id"], HabitCheckin(local_date=date(2026, 10, 8), completed=False))
    events = [event for event in repository.list_trace()
              if event["event_type"] == "habit_checkin_saved"]
    assert len(events) == 2 and all(event["actor"] == "user" for event in events)
    assert len(service.list_habit_checkins(habit["id"])) == 1


def test_existing_pages_manage_recurrence_and_habit_through_service(tmp_path, monkeypatch):
    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/02_tasks.py").run(timeout=30)
    field(at.text_input, "重复任务标题").set_value("每日阅读")
    field(at.button, "确认并创建重复规则").click().run(timeout=30)
    assert not at.exception
    repository = Repository(path)
    rules = repository.list_recurrence_rules()
    assert len(rules) == 1
    rule_id = rules[0]["id"]
    field(at.button, "生成未来七天任务").click().run(timeout=30)
    assert not at.exception
    assert len(repository.list_recurrence_instances(rule_id)) == 7
    assert all(item["unassigned_reason"] == "no_preferred_time"
               for item in repository.list_recurrence_instances(rule_id))
    field(at.button, "暂停重复规则").click().run(timeout=30)
    assert repository.get_recurrence_rule(rule_id)["status"] == "paused"
    field(at.button, "恢复重复规则").click().run(timeout=30)
    assert repository.get_recurrence_rule(rule_id)["status"] == "active"
    field(at.text_input, "修改重复任务标题").set_value("每日精读")
    field(at.button, "保存重复规则").click().run(timeout=30)
    assert repository.get_recurrence_rule(rule_id)["title"] == "每日精读"

    at.switch_page("pages/05_memory.py").run(timeout=30)
    field(at.text_input, "新习惯名称").set_value("运动")
    field(at.button, "创建习惯").click().run(timeout=30)
    assert not at.exception
    habits = repository.list_habits()
    assert len(habits) == 1
    habit_id = habits[0]["id"]
    field(at.text_input, "打卡备注").set_value("已完成")
    field(at.button, "保存或更正打卡").click().run(timeout=30)
    assert not at.exception
    checks = repository.list_habit_checkins(habit_id)
    assert len(checks) == 1 and checks[0]["completed"] == 1
    field(at.checkbox, "已完成").set_value(False)
    field(at.text_input, "打卡备注").set_value("更正")
    field(at.button, "保存或更正打卡").click().run(timeout=30)
    checks = repository.list_habit_checkins(habit_id)
    assert len(checks) == 1 and checks[0]["completed"] == 0 and checks[0]["note"] == "更正"
    field(at.button, "暂停习惯").click().run(timeout=30)
    assert repository.get_habit(habit_id)["status"] == "paused"
    field(at.button, "恢复习惯").click().run(timeout=30)
    assert repository.get_habit(habit_id)["status"] == "active"
