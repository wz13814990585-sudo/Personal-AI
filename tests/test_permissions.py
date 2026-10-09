import json

import pytest

from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.contracts import TaskCreate
from personal_ai_os.storage import Repository
from personal_ai_os.tool_gateway import ToolGateway


@pytest.fixture
def setup(tmp_path):
    repository = Repository(tmp_path / "db.sqlite3")
    repository.initialize()
    registry = AgentRegistry(repository)
    return repository, registry, ToolGateway(repository, registry)


def test_five_roles_and_permission_ceiling_survive_reopen(setup):
    repository, registry, _ = setup
    assert {item["role"] for item in registry.list_roles()} == {
        "orchestrator", "memory", "learning", "life", "schedule", "task"
    }
    with pytest.raises(PermissionError, match="maximum"):
        registry.update_role_instruction("memory", "Write tasks", {"commit_approved_plan"})
    updated = registry.update_role_instruction("memory", "Read approved memories", {"search_approved_memories"})
    assert updated["version"] == 2
    assert AgentRegistry(repository).get_role("memory")["tool_subset"] == ["search_approved_memories"]


def test_denied_write_leaves_business_data_unchanged_and_records_trace(setup):
    repository, _, gateway = setup
    repository.create_task(TaskCreate(title="Keep me"))
    before = repository.list_tasks()
    with pytest.raises(PermissionError, match="role_not_allowed"):
        gateway.bind("memory").invoke("commit_approved_plan", {"run_id": "run", "revision": 0})
    with pytest.raises(PermissionError, match="approval_required"):
        gateway.bind("task").invoke("commit_approved_plan", {"run_id": "run", "revision": 0})
    with pytest.raises(PermissionError, match="role_not_allowed"):
        gateway.bind("schedule").invoke("search_approved_memories", {})
    with pytest.raises(PermissionError, match="unknown_tool"):
        gateway.bind("memory").invoke("write_memory", {})
    assert repository.list_tasks() == before
    assert repository.list_approved_memories() == []
    events = repository.list_trace()
    assert [event["event_type"] for event in events] == ["tool_denied"] * 4
    assert [json.loads(event["summary_json"])["reason"] for event in events] == [
        "role_not_allowed", "approval_required", "role_not_allowed", "unknown_tool"
    ]


def test_bound_read_and_parameter_schema(setup):
    repository, _, gateway = setup
    repository.create_task(TaskCreate(title="Read me"))
    result = gateway.bind("task").invoke("read_tasks", {})
    assert [row["title"] for row in result.items] == ["Read me"]
    with pytest.raises(PermissionError, match="invalid_arguments"):
        gateway.bind("task").invoke("read_tasks", {"made_up_field": 1})
    assert repository.list_trace()[-1]["event_type"] == "tool_denied"
