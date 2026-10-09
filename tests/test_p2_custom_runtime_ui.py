"""Review and manual read-only execution stay inside the existing five pages."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from personal_ai_os import custom_agent_runtime
from personal_ai_os.contracts import (
    CustomAdviceItem, CustomAgentAdvice, CustomAgentProposalOutput,
)
from personal_ai_os.custom_agent_store import CustomAgentStore
from personal_ai_os.storage import Repository


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


class UiFakeRunner:
    def __init__(self, config):
        self.config = config
        self._usage = []

    def _request(self):
        self._usage = [{
            "model_id": "deepseek-flash", "input_tokens": 8,
            "output_tokens": 6, "total_tokens": 14, "duration_ms": 5,
        }]

    def consume_usage(self):
        result, self._usage = self._usage, []
        return result

    def propose(self, goal, allowed_tools, existing_names):
        self._request()
        return CustomAgentProposalOutput(
            name="Goal reviewer", description="Review goals",
            instructions="Only read goals and suggest a next step.",
            suggested_tools=["read_goals"], explanation="The goal needs periodic review.",
        )

    def advise(self, agent, request, tools):
        self._request()
        tools.invoke("read_goals", {})
        return CustomAgentAdvice(
            summary="Read-only review",
            recommendations=[CustomAdviceItem(action="Review goal", reason="User asked")],
        )


def test_console_review_manual_run_and_trace(tmp_path, monkeypatch):
    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setattr(custom_agent_runtime, "AgnoCustomRunner", UiFakeRunner)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/03_agents.py").run(timeout=30)
    field(at.text_area, "希望新增 Agent 处理的目标").set_value("Review goals")
    field(at.button, "生成待审核 Agent 提案").click().run(timeout=30)
    assert not at.exception
    repo = Repository(path)
    store = CustomAgentStore(repo)
    proposal = store.list_proposals("pending")[0]
    assert repo.list_custom_agents() == []

    field(at.text_input, "提案 Agent 名称").set_value("My goal reviewer")
    field(at.button, "审核并保存为暂停 Agent").click().run(timeout=30)
    assert not at.exception
    agent = repo.list_custom_agents()[0]
    assert agent["name"] == "My goal reviewer" and agent["status"] == "paused"
    assert store.get_proposal(proposal["id"])["status"] == "accepted"
    assert not any(button.label == "手动运行只读 Agent" for button in at.button)

    field(at.button, "启用自定义 Agent").click().run(timeout=30)
    assert not at.exception
    field(at.text_area, "给自定义 Agent 的只读请求").set_value("Read goals")
    field(at.button, "手动运行只读 Agent").click().run(timeout=30)
    assert not at.exception
    run = store.list_runs()[0]
    assert run["kind"] == "advice" and run["status"] == "success"
    assert [event["event_type"] for event in store.list_events(run["id"])] == [
        "run_started", "tool_called", "run_succeeded"
    ]

    at.switch_page("pages/04_trace.py").run(timeout=30)
    assert not at.exception
    assert field(at.selectbox, "选择自定义 Agent 运行").value == run["id"]
    assert any("自定义 Agent 运行" in item.value for item in at.markdown)
