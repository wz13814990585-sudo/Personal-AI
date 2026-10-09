"""Opt-in real DeepSeek review and manual read-only custom Agent run."""

import os

import pytest

from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.agents import MissingDeepSeekRunner
from personal_ai_os.config import load_config
from personal_ai_os.contracts import CustomAgentCreate, GoalCreate
from personal_ai_os.harness import Harness
from personal_ai_os.services import PersonalAIService
from personal_ai_os.storage import Repository
from personal_ai_os.tool_gateway import ToolGateway


@pytest.mark.skipif(
    os.environ.get("RUN_DEEPSEEK_SMOKE") != "1" or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY for real P2 acceptance",
)
def test_real_custom_agent_proposal_review_and_read_only_advice(tmp_path):
    config = load_config(require_model=True)
    repo = Repository(tmp_path / "p2-live.sqlite3")
    repo.initialize()
    registry = AgentRegistry(repo)
    gateway = ToolGateway(repo, registry, config.timezone)
    service = PersonalAIService(
        repo, gateway, Harness(repo, registry, gateway, MissingDeepSeekRunner(), config)
    )
    goal = service.create_goal(GoalCreate(title="系统学习 AI Agent 架构"))
    proposal = service.generate_custom_agent_proposal(
        "请设计一个每周回顾学习目标的只读 Agent；建议只能读取目标，不能写任务或记忆。"
    )
    assert proposal["status"] == "pending"
    assert repo.list_custom_agents() == []
    agent = service.accept_custom_agent_proposal(
        proposal["id"], proposal["revision"], CustomAgentCreate(
            name="Weekly goal reviewer", description="Review saved learning goals",
            instructions="先调用 read_goals 读取真实目标，再给出一条具体的学习建议；不要写入数据。",
            tool_subset={"read_goals"},
        ),
    )
    assert agent["status"] == "paused"
    active = service.set_custom_agent_status(agent["id"], agent["version"], "active")
    before_tasks = repo.list_tasks()
    before_memories = repo.list_approved_memories()
    run = service.run_custom_agent(
        active["id"], "请先调用 read_goals，依据已保存的学习目标给我一个下周行动建议。"
    )
    assert run["status"] == "success" and run["output"]["summary"]
    assert run["output"]["recommendations"]
    assert any(event["event_type"] == "tool_called"
               for event in service.list_custom_agent_trace(run["id"]))
    assert service.list_custom_agent_usage(run["id"])
    assert service.usage_summary()["request_count"] >= 2
    assert repo.list_tasks() == before_tasks
    assert repo.list_approved_memories() == before_memories
    assert service.list_goals()[0]["id"] == goal["id"]
