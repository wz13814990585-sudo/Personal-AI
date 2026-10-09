"""Existing today page reviews sourced advice and strategy rollback via Service."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from streamlit.testing.v1 import AppTest

from personal_ai_os.contracts import TaskCreate
from personal_ai_os.storage import Repository


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


def test_proactive_review_reject_and_rollback_in_today_page(tmp_path, monkeypatch):
    path = tmp_path / "proactive-ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    repo = Repository(path)
    repo.initialize()
    first = repo.create_task(TaskCreate(
        title="Interview preparation", due_at=datetime.now(timezone.utc) - timedelta(days=3),
    ))
    at = AppTest.from_file(APP).run(timeout=30)
    field(at.button, "扫描主动建议").click().run(timeout=30)
    assert not at.exception
    suggestions = repo.connection()
    with suggestions as connection:
        row = connection.execute("SELECT id,status,revision FROM proactive_suggestions").fetchone()
    assert row is not None and row["status"] == "pending"
    assert field(at.selectbox, "选择待审核主动建议").value == row["id"]
    assert any("可核查的来源" in item.label for item in at.expander)
    field(at.text_input, "修改建议标题").set_value("Check interview preparation")
    field(at.button, "保存建议修改").click().run(timeout=30)
    assert not at.exception
    with repo.connection() as connection:
        assert connection.execute("SELECT revision FROM proactive_suggestions WHERE id=?",
                                  (row["id"],)).fetchone()[0] == 1
    field(at.button, "接受建议并应用策略").click().run(timeout=30)
    assert not at.exception
    with repo.connection() as connection:
        accepted = connection.execute(
            "SELECT status,accepted_strategy_version FROM proactive_suggestions WHERE id=?",
            (row["id"],),
        ).fetchone()
        assert tuple(accepted) == ("accepted", 2)
    assert repo.get_task(first["id"])["status"] == "pending"
    assert repo.list_approved_memories() == []
    field(at.button, "回退建议策略").click().run(timeout=30)
    assert not at.exception
    with repo.connection() as connection:
        assert connection.execute("SELECT active_version FROM suggestion_strategy_state").fetchone()[0] == 3

    second = repo.create_task(TaskCreate(
        title="Laundry", due_at=datetime.now(timezone.utc) - timedelta(days=4),
    ))
    at.run(timeout=30)
    field(at.button, "扫描主动建议").click().run(timeout=30)
    assert not at.exception
    field(at.button, "拒绝主动建议").click().run(timeout=30)
    assert not at.exception
    with repo.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM proactive_suggestions").fetchone()[0]
    field(at.button, "扫描主动建议").click().run(timeout=30)
    with repo.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM proactive_suggestions").fetchone()[0] == count
    assert repo.get_task(second["id"])["status"] == "pending"
