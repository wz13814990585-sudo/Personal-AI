"""Opt-in real DeepSeek path: selected custom advice precedes built-in planning."""

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.agents import AgnoAgentRunner
from personal_ai_os.config import load_config
from personal_ai_os.contracts import CustomAgentCreate, GoalCreate, TaskCreate, TimeBlockCreate
from personal_ai_os.harness import Harness
from personal_ai_os.services import PersonalAIService
from personal_ai_os.storage import Repository
from personal_ai_os.tool_gateway import ToolGateway


@pytest.mark.skipif(
    os.environ.get("RUN_DEEPSEEK_SMOKE") != "1" or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY for live P2 planning",
)
def test_real_selected_custom_agent_then_five_agent_plan(tmp_path):
    config = load_config(require_model=True)
    zone = ZoneInfo(config.timezone)
    tomorrow = datetime.now(zone).date() + timedelta(days=1)
    repo = Repository(tmp_path / "p2-custom-planning-live.sqlite3")
    repo.initialize()
    repo.create_goal(GoalCreate(title="准备 AI Agent 面试"))
    repo.create_time_block(TimeBlockCreate(
        kind="available", start_at=datetime.combine(tomorrow, time(14), zone),
        end_at=datetime.combine(tomorrow, time(18), zone),
    ))
    registry = AgentRegistry(repo)
    gateway = ToolGateway(repo, registry, config.timezone)
    service = PersonalAIService(
        repo, gateway, Harness(repo, registry, gateway, AgnoAgentRunner(config, registry), config),
    )
    saved = service.create_custom_agent(CustomAgentCreate(
        name="Interview goal reviewer", description="Read current goals and advise",
        instructions="请先调用 read_goals，给出面试准备的一条只读建议；引用 ID 只能是实际读到的目标 ID，不要写数据。",
        tool_subset={"read_goals"},
    ))
    active = service.set_custom_agent_status(saved["id"], saved["version"], "active")
    try:
        draft = service.start_plan(
            "明晚七点有 AI Agent 面试，请制定学习计划并安排在明天下午两点到六点的可用时间内",
            custom_steps=[{"step_id": "custom_1", "agent_id": active["id"], "depends_on": []}],
        )
    except ValueError as exc:
        if str(exc) != "invalid_model_output":
            raise
        failed = service.list_runs()[0]
        assert failed["status"] == "failed" and repo.list_tasks() == []
        assert any(event["event_type"] == "run_failed"
                   for event in service.list_trace(failed["id"]))
        # The product asks the user to retry; invalid JSON is never retried silently.
        for _ in range(2):
            try:
                draft = service.retry_run(failed["id"])
                break
            except ValueError as retry_error:
                if str(retry_error) != "invalid_model_output":
                    raise
                failed = service.list_runs()[0]
                assert failed["status"] == "failed" and repo.list_tasks() == []
        else:
            pytest.fail("DeepSeek returned invalid JSON in three user-triggered runs")
    assert repo.get_run(draft.run_id)["status"] == "waiting_approval"
    assert [step["agent"] for step in service.list_run_steps(draft.run_id)] == [
        active["id"], "orchestrator", "memory", "learning", "schedule", "task",
    ]
    assert all(step["status"] == "success" for step in service.list_run_steps(draft.run_id))
    assert draft.tasks and repo.list_tasks() == []
    custom_run = service.list_custom_agent_runs()[0]
    assert custom_run["status"] == "success" and service.list_custom_agent_usage(custom_run["id"])
    assert service.list_model_usage(draft.run_id)
    assert any(event["event_type"] == "custom_graph_completed"
               for event in service.list_trace(draft.run_id))

    # P2-4 suggestions use local evidence and preserve this real DeepSeek approval path.
    overdue = repo.create_task(TaskCreate(
        title="整理面试笔记", due_at=datetime.now(zone) - timedelta(days=3),
    ))
    suggestions = service.scan_proactive_suggestions()
    selected = next(item for item in suggestions if item["subject_id"] == overdue["id"])
    assert selected["status"] == "pending" and repo.get_task(overdue["id"])["status"] == "pending"
    accepted = service.resolve_proactive_suggestion(selected["id"], selected["revision"], True)
    assert accepted["accepted_strategy_version"] == 2
    assert repo.get_task(overdue["id"])["status"] == "pending"
    assert repo.list_approved_memories() == []
    assert service.rollback_suggestion_strategy(1, 2)["version"] == 3
