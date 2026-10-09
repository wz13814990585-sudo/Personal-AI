"""The existing five pages expose reviewable recurrence and reschedule proposals."""

import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

from personal_ai_os.contracts import TaskCreate
from personal_ai_os.daily_planning import list_daily_plans
from test_p1_life_plan import LifeRunner, MIXED, system
from test_p1_daily_ui import setup


APP = str(Path(__file__).resolve().parents[1] / "app.py")
ZONE = ZoneInfo("Australia/Sydney")


def widget(items, label):
    return next(item for item in items if item.label == label)


def test_today_page_requires_explicit_weekday_rule_confirmation(tmp_path, monkeypatch):
    repository, _, _, service = system(tmp_path, LifeRunner())
    draft = service.start_plan(MIXED)
    monkeypatch.setenv("DATABASE_PATH", str(repository.path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    at = AppTest.from_file(APP).run(timeout=30)
    assert not at.exception
    assert widget(at.multiselect, "确认每周三个星期").value == ["周一", "周三", "周五"]
    assert repository.list_recurrence_rules() == [] and repository.list_tasks() == []
    widget(at.multiselect, "确认每周三个星期").set_value(["周二", "周四", "周六"])
    widget(at.button, "确认并创建建议的重复规则").click().run(timeout=30)
    assert not at.exception
    rules = repository.list_recurrence_rules()
    assert len(rules) == 1 and json.loads(rules[0]["weekdays_json"]) == [1, 3, 5]
    assert repository.list_tasks() == []
    assert any("已确认规则" in item.value for item in at.caption)
    assert repository.get_run(draft.run_id)["status"] == "waiting_approval"


def test_ui_availability_change_shows_old_new_times_before_confirmation(tmp_path, monkeypatch):
    repository, at, day = setup(tmp_path, monkeypatch)
    old_start = datetime.combine(day, time(9), ZONE)
    old_end = datetime.combine(day, time(10), ZONE)
    task = repository.create_task(TaskCreate(
        title="需要重排的运动", estimated_minutes=60,
        start_at=old_start, end_at=old_end,
    ))
    at.switch_page("pages/02_tasks.py").run(timeout=30)
    widget(at.text_input, "修改时间开始").set_value(datetime.combine(day, time(11), ZONE).isoformat())
    widget(at.text_input, "修改时间结束").set_value(datetime.combine(day, time(13), ZONE).isoformat())
    widget(at.button, "保存时间段").click().run(timeout=30)
    assert not at.exception
    assert repository.get_task(task["id"])["start_at_utc"] == old_start.astimezone(ZoneInfo("UTC")).isoformat()
    assert len(list_daily_plans(repository)) == 1
    at.switch_page("pages/01_today.py").run(timeout=30)
    at.date_input[0].set_value(day).run(timeout=30)
    assert not at.exception
    draft = list_daily_plans(repository)[0]
    assert len(draft.actions) == 1 and draft.actions[0].old_start_at == old_start
    assert draft.actions[0].new_start_at.astimezone(ZONE).hour == 11
    rendered = "\n".join(frame.value.to_string() for frame in at.dataframe)
    assert "需要重排的运动" in rendered and "原开始" in rendered and "新开始" in rendered
    assert repository.get_task(task["id"])["start_at_utc"] == old_start.astimezone(ZoneInfo("UTC")).isoformat()
    widget(at.button, "确认每日计划").click().run(timeout=30)
    assert not at.exception
    assert repository.get_task(task["id"])["start_at_utc"] == datetime.combine(
        day, time(11), ZONE
    ).astimezone(ZoneInfo("UTC")).isoformat()
