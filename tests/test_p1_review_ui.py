"""Existing five pages expose reviews, inbox and opt-in reminder controls via Service."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from streamlit.testing.v1 import AppTest

from personal_ai_os.contracts import TaskCreate, TimeBlockCreate
from personal_ai_os.storage import Repository
from personal_ai_os.worker import Worker, list_daily_reviews, list_notifications


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


def test_existing_pages_review_and_reminder_controls(tmp_path, monkeypatch):
    path = tmp_path / "review_ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    repo = Repository(path)
    repo.initialize()
    at = AppTest.from_file(APP).run(timeout=30)
    assert not at.exception
    field(at.button, "创建当日复盘").click().run(timeout=30)
    field(at.text_area, "复盘备注").set_value("只作为复盘备注")
    field(at.button, "保存复盘备注").click().run(timeout=30)
    assert not at.exception
    assert list_daily_reviews(repo)[0]["note"] == "只作为复盘备注"
    assert repo.list_memory_proposals() == [] and repo.list_approved_memories() == []

    at.switch_page("pages/05_memory.py").run(timeout=30)
    field(at.text_input, "静默开始（HH:MM）").set_value("23:00")
    field(at.text_input, "静默结束（HH:MM）").set_value("06:00")
    field(at.checkbox, "显式开启本机系统通知").set_value(False)
    field(at.button, "保存提醒偏好").click().run(timeout=30)
    assert not at.exception
    assert repo.get_setting("quiet_start") == "23:00"

    start = datetime.now(timezone.utc) + timedelta(minutes=11)
    repo.create_time_block(TimeBlockCreate(kind="available", start_at=start,
                                           end_at=start + timedelta(minutes=30)))
    task = repo.create_task(TaskCreate(title="AppTest 提醒", start_at=start,
                                       end_at=start + timedelta(minutes=30)))
    at.switch_page("pages/02_tasks.py").run(timeout=30)
    field(at.checkbox, "启用任务提醒").set_value(True)
    field(at.number_input, "提前提醒分钟").set_value(10)
    field(at.button, "保存提醒设置").click().run(timeout=30)
    assert not at.exception
    with repo.connection() as connection:
        saved = connection.execute("SELECT lead_minutes,enabled FROM task_reminders WHERE task_id=?",
                                   (task["id"],)).fetchone()
    assert tuple(saved) == (10, 1)
    # Worker is independent of the page. A due notification becomes visible in the inbox.
    repo.set_setting("quiet_start", "00:00")
    repo.set_setting("quiet_end", "00:00")
    Worker(repo).run_once(start - timedelta(minutes=10))
    assert len(list_notifications(repo)) == 1
    at.switch_page("pages/01_today.py").run(timeout=30)
    assert not at.exception
    rendered = "\n".join(item.value.to_string() for item in at.dataframe)
    assert "AppTest 提醒" in rendered
    field(at.button, "标记提醒已读").click().run(timeout=30)
    assert list_notifications(repo)[0]["read_at_utc"] is not None

    reopened = Repository(path)
    reopened.initialize()
    assert list_daily_reviews(reopened)[0]["note"] == "只作为复盘备注"
