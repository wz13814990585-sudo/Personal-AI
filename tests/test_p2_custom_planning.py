"""Explicit custom planning, parallel read-only nodes and failure barriers."""

import json
import threading
from dataclasses import replace

import pytest

from personal_ai_os.contracts import CustomAgentAdvice, CustomAgentCreate
from personal_ai_os.services import PersonalAIService


class AdviceRunner:
    def __init__(self):
        self.barrier = None
        self.fail_name = None
        self.started = []
        self.finished = []
        self.lock = threading.Lock()
        self.beta_done = threading.Event()

    def consume_usage(self):
        return []

    def advise(self, agent, request, tools):
        with self.lock:
            self.started.append(agent["name"])
        if self.barrier and agent["name"] in {"Alpha", "Beta"}:
            self.barrier.wait(timeout=3)  # Fails if independent steps run serially.
        if agent["name"] == self.fail_name:
            raise TimeoutError("injected custom timeout")
        tools.invoke("read_goals", {})
        if agent["name"] == "Alpha" and self.barrier:
            assert self.beta_done.wait(timeout=3)
        with self.lock:
            self.finished.append(agent["name"])
        if agent["name"] == "Beta":
            self.beta_done.set()
        return CustomAgentAdvice(summary=f"{agent['name']} advice")


def setup(flow_system):
    repo, registry, gateway, builtin, harness, _ = flow_system
    harness.config = replace(harness.config, deepseek_api_key="test-only")
    custom = AdviceRunner()
    service = PersonalAIService(repo, gateway, harness, registry, custom_runner=custom)
    agents = []
    for name in ("Alpha", "Beta", "Gamma"):
        saved = service.create_custom_agent(CustomAgentCreate(
            name=name, instructions="Read goals and advise only.",
            tool_subset={"read_goals"},
        ))
        agents.append(service.set_custom_agent_status(saved["id"], saved["version"], "active"))
    return repo, service, custom, builtin, agents


def step(number, agent, dependencies=None):
    return {"step_id": f"custom_{number}", "agent_id": agent["id"],
            "depends_on": dependencies or []}


def test_parallel_independent_nodes_stable_merge_then_dependent_and_approval(flow_system):
    repo, service, custom, builtin, agents = setup(flow_system)
    custom.barrier = threading.Barrier(2)
    draft = service.start_plan("Prepare for my interview", custom_steps=[
        step(1, agents[0]), step(2, agents[1]), step(3, agents[2], ["custom_1", "custom_2"]),
    ])
    assert custom.finished.index("Beta") < custom.finished.index("Alpha")
    assert custom.started.index("Gamma") > custom.started.index("Beta")
    steps = service.list_run_steps(draft.run_id)
    assert [item["step_id"] for item in steps[:3]] == ["custom_1", "custom_2", "custom_3"]
    assert all(item["status"] == "success" for item in steps)
    assert [item["agent"] for item in steps[3:]] == [
        "orchestrator", "memory", "learning", "schedule", "task",
    ]
    custom_context = builtin.calls[0][2]["selected_custom_advice"]
    assert [item["step_id"] for item in custom_context] == [
        "custom_1", "custom_2", "custom_3",
    ]
    assert "Alpha advice" in custom_context[0]["advice"]["summary"]
    assert "Beta advice" in custom_context[1]["advice"]["summary"]
    assert "Alpha" in custom.started and "Beta" in custom.started
    assert repo.list_tasks() == []
    assert repo.list_approved_memories() == []
    assert [item["event_type"] for item in service.list_trace(draft.run_id)
            if item["event_type"] == "custom_step_succeeded"] == [
                "custom_step_succeeded", "custom_step_succeeded", "custom_step_succeeded",
            ]
    assert all(json.loads(item["result_json"])["custom_run_id"]
               for item in steps[:3])
    committed = service.approve_plan(draft.run_id, draft.revision)
    assert committed and len(repo.list_tasks()) == len(committed)


def test_failed_step_skips_dependency_blocks_builtin_and_retry(flow_system):
    repo, service, custom, builtin, agents = setup(flow_system)
    custom.fail_name = "Alpha"
    with pytest.raises(RuntimeError, match="custom_graph_failed:custom_1"):
        service.start_plan("Prepare for my interview", custom_steps=[
            step(1, agents[0]), step(2, agents[1]), step(3, agents[2], ["custom_1"]),
        ])
    run = service.list_runs()[0]
    statuses = {item["step_id"]: item for item in service.list_run_steps(run["id"])}
    assert run["status"] == "failed" and run["draft_json"] is None
    assert statuses["custom_1"]["status"] == "failed"
    assert statuses["custom_2"]["status"] == "success"
    assert statuses["custom_3"]["status"] == "skipped"
    assert statuses["custom_3"]["error_code"] == "dependency_failed"
    assert builtin.calls == [] and repo.list_tasks() == []
    custom.fail_name = None
    draft = service.retry_run(run["id"])
    assert draft.run_id != run["id"] and repo.get_run(run["id"])["status"] == "failed"
    assert len(service.list_run_steps(draft.run_id)) == 8


def test_invalid_graph_and_permissions_never_call_model(flow_system):
    repo, service, custom, builtin, agents = setup(flow_system)
    cases = [
        ([step(1, agents[0]), step(1, agents[1])], ValueError),
        ([step(1, agents[0], ["custom_2"]),
          step(2, agents[1], ["custom_1"])], ValueError),
        ([step(1, agents[0], ["unknown"])], ValueError),
        ([step(1, agents[0]), step(2, agents[0])], ValueError),
        ([step(1, agents[0])] * 5, ValueError),
    ]
    for selected, error in cases:
        with pytest.raises(error):
            service.start_plan("Prepare", custom_steps=selected)
    service.set_custom_agent_status(agents[1]["id"], agents[1]["version"], "paused")
    with pytest.raises(PermissionError):
        service.start_plan("Prepare", custom_steps=[step(1, agents[1])])
    with repo.transaction() as connection:
        connection.execute(
            "UPDATE custom_agents SET tool_subset_json=? WHERE id=?",
            (json.dumps(["commit_approved_plan"]), agents[0]["id"]),
        )
    with pytest.raises(PermissionError):
        service.start_plan("Prepare", custom_steps=[step(1, agents[0])])
    assert repo.list_runs() == [] and custom.started == [] and builtin.calls == []


def test_default_path_unchanged_and_custom_timeout_fails_closed(flow_system):
    repo, service, custom, builtin, agents = setup(flow_system)
    draft = service.start_plan("Prepare for my interview")
    assert "selected_custom_advice" not in builtin.calls[0][2]
    assert all(not item["step_id"].startswith("custom_")
               for item in service.list_run_steps(draft.run_id))
    service.harness.config = replace(service.harness.config, model_timeout_seconds=0)
    service.custom_runtime.config = service.harness.config
    with pytest.raises(RuntimeError, match="custom_graph_failed"):
        service.start_plan("Another interview", custom_steps=[step(1, agents[0])])
    assert service.list_runs()[0]["status"] == "failed"
    assert len(repo.list_tasks()) == 0


def test_unpersisted_custom_context_cannot_enter_builtin_harness(flow_system):
    repo, service, custom, builtin, agents = setup(flow_system)
    run = repo.create_run("Prepare for my interview")
    with pytest.raises(ValueError, match="run_not_pending_planning"):
        service.harness.execute_plan(run["id"], custom_context=[{
            "step_id": "custom_1", "agent_id": agents[0]["id"],
            "advice": {"summary": "fabricated"},
        }])
    assert builtin.calls == [] and repo.list_tasks() == []
