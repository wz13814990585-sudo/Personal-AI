"""The existing Agent console manages P2 definitions through the Service."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from personal_ai_os.storage import Repository


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


def test_agent_console_create_edit_pause_and_resume(tmp_path, monkeypatch):
    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/03_agents.py").run(timeout=30)
    assert not at.exception
    field(at.text_input, "新 Agent 名称").set_value("Study reviewer")
    field(at.text_area, "新 Agent 指令").set_value("Summarize progress only")
    field(at.multiselect, "新 Agent 只读工具").set_value(["read_task_progress"])
    field(at.button, "创建自定义 Agent").click().run(timeout=30)
    assert not at.exception
    repo = Repository(path)
    agent = repo.list_custom_agents()[0]
    assert agent["name"] == "Study reviewer" and agent["status"] == "paused"
    assert agent["tool_subset"] == ["read_task_progress"]
    field(at.button, "启用自定义 Agent").click().run(timeout=30)
    assert not at.exception
    assert repo.get_custom_agent(agent["id"])["status"] == "active"
    field(at.text_input, "自定义 Agent 名称").set_value("Progress reviewer")
    field(at.button, "保存自定义 Agent").click().run(timeout=30)
    assert not at.exception
    assert repo.get_custom_agent(agent["id"])["name"] == "Progress reviewer"
    field(at.button, "暂停自定义 Agent").click().run(timeout=30)
    assert not at.exception
    assert repo.get_custom_agent(agent["id"])["status"] == "paused"
    assert len(repo.list_agent_configs()) == 6
