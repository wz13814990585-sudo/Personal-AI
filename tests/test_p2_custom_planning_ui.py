"""Selected custom planning is visible on today's page and the trace page."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from conftest import FlowRunner, moment
from personal_ai_os import custom_agent_runtime
from personal_ai_os.agents import AgnoAgentRunner
from personal_ai_os.contracts import CustomAgentAdvice, CustomAgentCreate, TimeBlockCreate
from personal_ai_os.storage import Repository


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


class UiCustomRunner:
    def __init__(self, config):
        self.config = config

    def consume_usage(self):
        return []

    def advise(self, agent, request, tools):
        tools.invoke("read_goals", {})
        return CustomAgentAdvice(summary=f"Advice from {agent['name']}")


def test_selected_custom_agents_plan_and_trace(tmp_path, monkeypatch):
    path = tmp_path / "custom-planning-ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    fake = FlowRunner()
    monkeypatch.setattr(
        AgnoAgentRunner, "run",
        lambda self, role, payload, schema, tools: fake.run(role, payload, schema, tools),
    )
    monkeypatch.setattr(custom_agent_runtime, "AgnoCustomRunner", UiCustomRunner)
    repo = Repository(path)
    repo.initialize()
    for day, hour in ((10, 22), (11, 4)):
        repo.create_time_block(TimeBlockCreate(
            kind="available", start_at=moment(day, hour), end_at=moment(day, hour + 1),
        ))
    agents = []
    for name in ("Goal reader", "Study adviser"):
        agent = repo.create_custom_agent(CustomAgentCreate(
            name=name, instructions="Only read and advise", tool_subset={"read_goals"},
        ))
        agents.append(repo.set_custom_agent_status(agent["id"], agent["version"], "active"))
    at = AppTest.from_file(APP).run(timeout=30)
    field(at.multiselect, "选用自定义 Agent 参与本次规划（只读）").set_value(
        [item["id"] for item in agents]
    ).run(timeout=30)
    field(at.multiselect, "依赖前置步骤 · Study adviser").set_value(
        ["custom_1"]
    ).run(timeout=30)
    field(at.text_area, "你的目标或请求").set_value(
        "明晚七点有 AI Agent 面试，请制定学习计划并安排任务"
    )
    field(at.button, "生成计划").click().run(timeout=30)
    assert not at.exception
    run = repo.list_runs()[0]
    assert run["status"] == "waiting_approval" and repo.list_tasks() == []
    steps = repo.list_run_steps(run["id"])
    assert [item["status"] for item in steps[:2]] == ["success", "success"]
    assert [item["step_id"] for item in steps[:2]] == ["custom_1", "custom_2"]
    assert fake.calls[0][2]["selected_custom_advice"][1]["advice"]["summary"] == (
        "Advice from Study adviser"
    )
    at.switch_page("pages/04_trace.py").run(timeout=30)
    assert not at.exception
    assert any("自定义 Agent 运行编号" in item.value for item in at.caption)
