"""Opt-in, billable real-model check of the five-Agent planning path."""

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.agents import AgnoAgentRunner
from personal_ai_os.config import load_config
from personal_ai_os.contracts import TimeBlockCreate
from personal_ai_os.contracts import TaskCreate
from personal_ai_os.harness import Harness
from personal_ai_os.services import PersonalAIService
from personal_ai_os.storage import Repository
from personal_ai_os.tool_gateway import ToolGateway


@pytest.mark.skipif(
    os.environ.get("RUN_DEEPSEEK_SMOKE") != "1"
    or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY to use real DeepSeek",
)
def test_real_deepseek_five_agent_planning(tmp_path):
    config = load_config(require_model=True)
    zone = ZoneInfo(config.timezone)
    tomorrow = datetime.now(zone).date() + timedelta(days=1)
    repository = Repository(tmp_path / "live.sqlite3")
    repository.initialize()
    repository.create_time_block(TimeBlockCreate(
        kind="available",
        start_at=datetime.combine(tomorrow, time(14), zone),
        end_at=datetime.combine(tomorrow, time(18), zone),
    ))
    registry = AgentRegistry(repository)
    runner = AgnoAgentRunner(config, registry)
    harness = Harness(repository, registry, ToolGateway(repository, registry), runner, config)
    run = repository.create_run(
        "明晚七点有 AI Agent 面试，请制定学习计划并安排在明天下午两点到六点的可用时间内"
    )
    draft = harness.execute_plan(run["id"])
    steps = repository.list_run_steps(run["id"])
    assert repository.get_run(run["id"])["status"] == "waiting_approval"
    assert [step["agent"] for step in steps] == [
        "orchestrator", "memory", "learning", "schedule", "task"
    ]
    assert all(step["status"] == "success" for step in steps)
    assert draft.tasks
    assert repository.list_tasks() == []
    assert len(repository.list_trace(run["id"])) >= 16


@pytest.mark.skipif(
    os.environ.get("RUN_DEEPSEEK_SMOKE") != "1"
    or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY to use real DeepSeek",
)
def test_real_deepseek_feedback_proposes_reviewable_memory(tmp_path):
    config = load_config(require_model=True)
    repository = Repository(tmp_path / "feedback_live.sqlite3")
    repository.initialize()
    registry = AgentRegistry(repository)
    gateway = ToolGateway(repository, registry, config.timezone)
    runner = AgnoAgentRunner(config, registry)
    service = PersonalAIService(
        repository, gateway, Harness(repository, registry, gateway, runner, config)
    )
    task = repository.create_task(TaskCreate(title="Prepare interview"))
    service.complete_task(task["id"])
    feedback, proposals = service.submit_feedback(
        task["id"], "今天学习不顺利。不要在早上安排学习，下午更合适。"
    )
    assert feedback["body"]
    assert proposals and proposals[0]["kind"] == "study_time_avoid"
    assert repository.list_approved_memories() == []
    memory = service.resolve_memory(proposals[0]["id"], approve=True)
    assert memory["kind"] == "study_time_avoid"
    assert len(repository.list_approved_memories()) == 1


@pytest.mark.skipif(
    os.environ.get("RUN_DEEPSEEK_SMOKE") != "1"
    or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY to use real DeepSeek",
)
def test_real_deepseek_interview_feedback_replan(tmp_path):
    """One real-model run crosses approval, feedback, review, and changed planning."""
    config = load_config(require_model=True)
    zone = ZoneInfo(config.timezone)
    tomorrow = datetime.now(zone).date() + timedelta(days=1)
    repository = Repository(tmp_path / "interview_live.sqlite3")
    repository.initialize()
    for start, end in ((8, 12), (14, 18)):
        repository.create_time_block(TimeBlockCreate(
            kind="available",
            start_at=datetime.combine(tomorrow, time(start), zone),
            end_at=datetime.combine(tomorrow, time(end), zone),
        ))
    registry = AgentRegistry(repository)
    gateway = ToolGateway(repository, registry, config.timezone)
    service = PersonalAIService(
        repository, gateway,
        Harness(repository, registry, gateway, AgnoAgentRunner(config, registry), config),
    )
    request = "明晚七点有 AI Agent 面试，请制定学习计划、安排任务并记住我的学习习惯"
    first = service.start_plan(request)
    assert [step["agent"] for step in service.list_run_steps(first.run_id)] == [
        "orchestrator", "memory", "learning", "schedule", "task"
    ]
    assert all(step["status"] == "success" for step in service.list_run_steps(first.run_id))
    assert first.tasks and repository.list_tasks() == []
    committed = service.approve_plan(first.run_id, first.revision)
    assert len(committed) == len(first.tasks)
    for task in committed:
        service.complete_task(task["id"])
    feedback, proposals = service.submit_feedback(
        committed[0]["id"], "今天准备面试时我发现：不要在早上安排学习，下午更合适。"
    )
    assert feedback["body"] and repository.list_approved_memories() == []
    avoid = next(item for item in proposals if item["kind"] == "study_time_avoid")
    memory = service.resolve_memory(avoid["id"], approve=True)
    assert memory["id"] in [item["id"] for item in repository.list_approved_memories()]
    second = service.start_plan(request)
    assert second.tasks and memory["id"] in second.memory_ids
    assert any(memory["id"] in explanation for explanation in second.explanations)
    scheduled = [task for task in second.tasks if task.start_at]
    assert scheduled
    assert all(task.start_at.astimezone(zone).hour >= 12 for task in scheduled)
    assert [step["agent"] for step in service.list_run_steps(second.run_id)] == [
        "orchestrator", "memory", "learning", "schedule", "task"
    ]
