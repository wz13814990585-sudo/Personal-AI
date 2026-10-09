import json
from datetime import datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

from personal_ai_os.agents import AgnoAgentRunner
from personal_ai_os.contracts import PlanDraft, TimeBlockCreate
from personal_ai_os.storage import Repository

from conftest import FlowRunner, moment


APP = str(Path(__file__).resolve().parents[1] / "app.py")
REQUEST = "明晚七点有 AI Agent 面试，请制定学习计划并安排任务"
FEEDBACK = "今天准备面试时我发现：不要在早上安排学习，下午更合适。"


def _field(items, label):
    return next(item for item in items if item.label == label)


def _assert_page(at, path, title):
    at.switch_page(path).run(timeout=30)
    assert not at.exception
    assert [item.value for item in at.title] == [title]


def test_five_pages_open_without_key_and_show_persisted_data(tmp_path, monkeypatch):
    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    at = AppTest.from_file(APP).run(timeout=30)
    for page, title in (
        ("pages/01_today.py", "对话与今日计划"),
        ("pages/02_tasks.py", "任务与日程"),
        ("pages/03_agents.py", "Agent 控制台"),
        ("pages/04_trace.py", "执行轨迹"),
        ("pages/05_memory.py", "记忆与设置"),
    ):
        _assert_page(at, page, title)
    assert {row["role"] for row in Repository(path).list_agent_configs()} == {
        "orchestrator", "memory", "learning", "life", "schedule", "task"
    }


def test_time_block_form_explains_invalid_range_and_accepts_valid_default(tmp_path, monkeypatch):
    path = tmp_path / "time_block_ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    at = AppTest.from_file(APP).run(timeout=30)
    _assert_page(at, "pages/02_tasks.py", "任务与日程")
    start = _field(at.text_input, "时间开始（ISO 8601）").value
    end = _field(at.text_input, "时间结束（ISO 8601）").value
    assert datetime.fromisoformat(end) > datetime.fromisoformat(start)

    _field(at.text_input, "时间结束（ISO 8601）").set_value(start)
    _field(at.button, "添加时间段").click().run(timeout=30)
    assert any("时间结束必须晚于时间开始" in error.value for error in at.error)
    assert Repository(path).list_time_blocks() == []

    _field(at.text_input, "时间结束（ISO 8601）").set_value(end)
    _field(at.button, "添加时间段").click().run(timeout=30)
    assert not at.exception
    assert len(Repository(path).list_time_blocks()) == 1


def test_web_flow_plan_confirm_feedback_review_and_replan(tmp_path, monkeypatch):
    path = tmp_path / "flow_ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    fake = FlowRunner()
    monkeypatch.setattr(
        AgnoAgentRunner, "run",
        lambda self, role, payload, schema, tools: fake.run(role, payload, schema, tools),
    )
    repository = Repository(path)
    repository.initialize()
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=moment(10, 22), end_at=moment(10, 23)
    ))
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=moment(11, 4), end_at=moment(11, 5)
    ))
    at = AppTest.from_file(APP).run(timeout=30)
    _field(at.text_area, "你的目标或请求").set_value(REQUEST)
    _field(at.button, "生成计划").click().run(timeout=30)
    assert not at.exception
    first_run = repository.list_runs()[0]
    assert first_run["status"] == "waiting_approval"
    assert repository.list_tasks() == []
    assert len(repository.list_run_steps(first_run["id"])) == 5
    _field(at.text_input, "标题").set_value("面试重点复习")
    _field(at.text_input, "开始时间（ISO 8601，可留空）").set_value("2026-10-11T05:00+11:00")
    _field(at.text_input, "结束时间（ISO 8601，可留空）").set_value("2026-10-11T06:00+11:00")
    _field(at.button, "保存计划修改").click().run(timeout=30)
    assert not at.exception
    edited = PlanDraft.model_validate_json(repository.get_run(first_run["id"])["draft_json"])
    assert edited.revision == 1 and edited.tasks[0].title == "面试重点复习"
    assert edited.tasks[0].start_at is not None and edited.conflicts
    _field(at.button, "确认并加入待办").click().run(timeout=30)
    assert repository.list_tasks() == []
    assert repository.get_run(first_run["id"])["status"] == "waiting_approval"
    _field(at.text_input, "开始时间（ISO 8601，可留空）").set_value(moment(10, 22).isoformat())
    _field(at.text_input, "结束时间（ISO 8601，可留空）").set_value(moment(10, 23).isoformat())
    _field(at.button, "保存计划修改").click().run(timeout=30)
    fixed = PlanDraft.model_validate_json(repository.get_run(first_run["id"])["draft_json"])
    assert fixed.revision == 2 and fixed.conflicts == []
    _field(at.button, "确认并加入待办").click().run(timeout=30)
    assert not at.exception
    assert len(repository.list_tasks()) == 1
    assert repository.list_tasks()[0]["title"] == "面试重点复习"
    assert any(event["event_type"] == "tool_called" for event in repository.list_trace(first_run["id"]))

    _assert_page(at, "pages/02_tasks.py", "任务与日程")
    _field(at.button, "标记任务完成").click().run(timeout=30)
    _field(at.text_area, "完成后的反馈").set_value(FEEDBACK)
    _field(at.button, "提交反馈并提取候选记忆").click().run(timeout=30)
    assert not at.exception
    assert len(repository.list_memory_proposals("pending")) == 1
    assert repository.list_approved_memories() == []

    _assert_page(at, "pages/05_memory.py", "记忆与设置")
    _field(at.text_input, "候选避让开始（HH:MM）").set_value("00:00")
    _field(at.text_input, "候选避让结束（HH:MM）").set_value("12:00")
    _field(at.button, "保存候选修改").click().run(timeout=30)
    assert json.loads(repository.list_memory_proposals("pending")[0]["value_json"]) == {
        "start": "00:00", "end": "12:00"
    }
    _field(at.button, "批准记忆").click().run(timeout=30)
    assert not at.exception
    memory = repository.list_approved_memories()[0]
    _assert_page(at, "pages/01_today.py", "对话与今日计划")
    _field(at.text_area, "你的目标或请求").set_value(REQUEST)
    _field(at.button, "生成计划").click().run(timeout=30)
    assert not at.exception
    second_run = repository.list_runs()[0]
    draft = PlanDraft.model_validate_json(second_run["draft_json"])
    assert draft.tasks[0].start_at == moment(11, 4)
    assert memory["id"] in draft.memory_ids
    orchestrator_inputs = [payload for role, _, payload in fake.calls if role == "orchestrator"]
    assert orchestrator_inputs[1]["recent_conversation"][0]["status"] == "success"
    assert "面试重点复习" in orchestrator_inputs[1]["recent_conversation"][0]["summary"]

    _assert_page(at, "pages/04_trace.py", "执行轨迹")
    assert [item.value for item in at.subheader] == ["步骤与依赖", "逐步事件"]
    assert any(event["event_type"] == "delegated" for event in repository.list_trace(second_run["id"]))

    _assert_page(at, "pages/05_memory.py", "记忆与设置")
    _field(at.text_input, "记忆避让结束（HH:MM）").set_value("10:00")
    _field(at.button, "保存记忆修改").click().run(timeout=30)
    assert json.loads(repository.list_approved_memories()[0]["value_json"])["end"] == "10:00"
    _field(at.button, "删除记忆").click().run(timeout=30)
    assert repository.list_approved_memories() == []


def test_web_reports_planning_failure_and_trace(tmp_path, monkeypatch):
    path = tmp_path / "failed_ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def timeout(*_args):
        raise TimeoutError("injected model timeout")

    monkeypatch.setattr(AgnoAgentRunner, "run", timeout)
    repository = Repository(path)
    at = AppTest.from_file(APP).run(timeout=30)
    _field(at.text_area, "你的目标或请求").set_value(REQUEST)
    _field(at.button, "生成计划").click().run(timeout=30)
    assert not at.exception
    run = repository.list_runs()[0]
    assert run["status"] == "failed" and repository.list_tasks() == []
    assert any(event["event_type"] == "run_failed" for event in repository.list_trace(run["id"]))
    _assert_page(at, "pages/04_trace.py", "执行轨迹")
    assert any("失败" in item.value for item in at.markdown)


def test_manual_goal_task_time_and_agent_config_controls(tmp_path, monkeypatch):
    path = tmp_path / "manual_ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    repository = Repository(path)
    repository.initialize()
    at = AppTest.from_file(APP).run(timeout=30)

    _assert_page(at, "pages/05_memory.py", "记忆与设置")
    _field(at.text_input, "新目标标题").set_value("准备 AI Agent 面试")
    _field(at.button, "添加目标").click().run(timeout=30)
    assert not at.exception
    assert repository.list_goals()[0]["title"] == "准备 AI Agent 面试"
    _field(at.text_input, "时区").set_value("Australia/Perth")
    _field(at.button, "保存设置").click().run(timeout=30)
    assert repository.get_setting("timezone") == "Australia/Perth"

    _assert_page(at, "pages/03_agents.py", "Agent 控制台")
    _field(at.text_area, "角色指令").set_value("规划依赖清晰的步骤")
    at.button[0].click().run(timeout=30)
    assert not at.exception
    assert repository.get_agent_config("orchestrator")["version"] == 2

    _assert_page(at, "pages/02_tasks.py", "任务与日程")
    _field(at.text_input, "任务标题").set_value("手动复习")
    _field(at.button, "创建任务").click().run(timeout=30)
    assert repository.list_tasks()[0]["title"] == "手动复习"
    _field(at.text_input, "时间开始（ISO 8601）").set_value("2026-10-11T14:00+08:00")
    _field(at.text_input, "时间结束（ISO 8601）").set_value("2026-10-11T15:00+08:00")
    _field(at.button, "添加时间段").click().run(timeout=30)
    assert not at.exception
    assert len(repository.list_time_blocks()) == 1
