"""Opt-in, billable DeepSeek mixed-domain end-to-end acceptance."""

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.agents import AgnoAgentRunner
from personal_ai_os.config import load_config
from personal_ai_os.contracts import TimeBlockCreate
from personal_ai_os.harness import Harness
from personal_ai_os.services import PersonalAIService
from personal_ai_os.storage import Repository
from personal_ai_os.tool_gateway import ToolGateway


@pytest.mark.skipif(
    os.environ.get("RUN_DEEPSEEK_SMOKE") != "1" or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY for the real P1 acceptance",
)
def test_real_mixed_goal_approval_feedback_memory_and_replan(tmp_path):
    config = load_config(require_model=True)
    zone = ZoneInfo(config.timezone)
    tomorrow = datetime.now(zone).date() + timedelta(days=1)
    repository = Repository(tmp_path / "real_p1.sqlite3")
    repository.initialize()
    for start, end in ((8, 12), (13, 19)):
        repository.create_time_block(TimeBlockCreate(
            kind="available", start_at=datetime.combine(tomorrow, time(start), zone),
            end_at=datetime.combine(tomorrow, time(end), zone),
        ))
    registry = AgentRegistry(repository)
    gateway = ToolGateway(repository, registry, config.timezone)
    service = PersonalAIService(
        repository, gateway,
        Harness(repository, registry, gateway, AgnoAgentRunner(config, registry), config),
    )
    request = (
        "明晚七点有 AI Agent 面试，请只制定一项面试学习任务；"
        "我还想每周三次运动，请只制定一项运动生活任务，合并为计划草案。"
        "明天上午八点到十二点、下午一点到七点可用，请尽量安排在可用时间里。"
    )
    try:
        first = service.start_plan(request)
    except ValueError:
        failed = repository.list_runs()[0]
        assert failed["status"] == "failed" and repository.list_tasks() == []
        first = service.retry_run(failed["id"])
    assert {task.domain for task in first.tasks} == {"study", "life"}
    assert repository.list_tasks() == []
    assert [step["agent"] for step in service.list_run_steps(first.run_id)] == [
        "orchestrator", "memory", "learning", "life", "schedule", "task"
    ]
    suggestions = service.list_recurrence_suggestions(first.run_id)
    assert len(suggestions) == 1 and suggestions[0]["weekdays"] == [0, 2, 4]
    assert repository.list_recurrence_rules() == []
    rule = service.confirm_recurrence_suggestion(
        first.run_id, first.revision, suggestions[0]["item_id"], [0, 2, 4], tomorrow + timedelta(days=1)
    )
    assert rule["frequency"] == "weekly_days"
    committed = service.approve_plan(first.run_id, first.revision)
    assert len(committed) == len(first.tasks)
    study_task = next(task for task in committed if task["domain"] == "study")
    service.complete_task(study_task["id"])
    _, proposals = service.submit_feedback(
        study_task["id"], "今天准备面试时我发现：不要在早上安排学习，下午更合适。"
    )
    assert proposals and repository.list_approved_memories() == []
    memory = service.resolve_memory(proposals[0]["id"], approve=True)
    assert memory is not None
    before_replan = len(repository.list_tasks())
    try:
        second = service.start_plan(request)
    except ValueError:
        # Invalid model JSON is a recorded failure, not an automatic retry.
        failed = repository.list_runs()[0]
        assert failed["status"] == "failed" and len(repository.list_tasks()) == before_replan
        second = service.retry_run(failed["id"])
    assert len(repository.list_tasks()) == before_replan
    assert memory["id"] in second.memory_ids
    assert {task.domain for task in second.tasks} == {"study", "life"}
    assert all(task.start_at is None or task.start_at.astimezone(zone).hour >= 12
               for task in second.tasks if task.domain == "study")
    summary = service.usage_summary()
    assert summary["request_count"] >= 13
    assert summary["request_count"] == len(service.list_model_usage())
    assert summary["total_tokens"] > 0 or summary["missing_token_records"] > 0
    assert summary["estimated_cost_usd"] is None
