"""YouTube controls and pending resource links on the existing five pages."""

from dataclasses import replace
from pathlib import Path

from streamlit.testing.v1 import AppTest

from test_p2_youtube_mcp import VIDEO, VIDEO_ID, allow, setup_youtube

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


def test_youtube_registration_search_and_planning_choice(flow_system, monkeypatch):
    repo, _, _, runner, harness, service = flow_system
    monkeypatch.setattr("personal_ai_os.bootstrap.page_service", lambda: service)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/03_agents.py").run(timeout=30)
    assert not at.exception
    field(at.button, "登记 YouTube 本机只读服务器").click().run(timeout=30)
    assert not at.exception
    entry = next(item for item in service.list_external_servers() if item["name"].startswith("YouTube"))
    assert entry["status"] == "paused"
    fake = setup_youtube(service)[1]
    field(at.text_input, "YouTube 学习资料搜索词").set_value("AI Agent")
    field(at.button, "搜索 YouTube 公开视频").click().run(timeout=30)
    assert not at.exception and fake.calls == []
    allow(service, entry)
    at.run(timeout=30)
    field(at.button, "搜索 YouTube 公开视频").click().run(timeout=30)
    assert not at.exception and len(fake.calls) == 2
    assert at.session_state["youtube_results"][0]["url"] == VIDEO["url"]
    harness.config = replace(harness.config, deepseek_api_key="test-only")
    original_run = runner.run
    def run(role, payload, schema, tools):
        result = original_run(role, payload, schema, tools)
        if role == "learning" and "youtube_resources" in payload:
            return result.model_copy(update={"items": [result.items[0].model_copy(update={"resource_video_id": VIDEO_ID})]})
        return result
    monkeypatch.setattr(runner, "run", run)
    at.switch_page("pages/01_today.py").run(timeout=30)
    assert not at.exception
    assert not field(at.checkbox, "本次使用 YouTube 公共视频资料（需先在 Agent 控制台启用两项只读操作）").value
    field(at.text_area, "你的目标或请求").set_value("学习 AI Agent 面试")
    field(at.text_input, "YouTube 搜索词（仅选择 YouTube 时使用；最多 120 字）").set_value("AI Agent interview")
    field(at.checkbox, "本次使用 YouTube 公共视频资料（需先在 Agent 控制台启用两项只读操作）").check()
    field(at.button, "生成计划").click().run(timeout=30)
    assert not at.exception
    assert repo.list_tasks() == []
    draft = service.get_plan(service.list_runs()[0]["id"])
    assert draft.tasks[0].resource_url == VIDEO["url"]
    assert draft.tasks[0].resource_channel == VIDEO["channel"]
