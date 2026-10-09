"""P2 round 1: safe custom Agent definitions without execution."""

import json
import sqlite3

import pytest

from personal_ai_os import migrations
from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.contracts import CustomAgentCreate, TaskCreate
from personal_ai_os.storage import Repository


def definition(name="Review helper", tools=None):
    return CustomAgentCreate(
        name=name, description="Summarize progress", instructions="Only advise the user.",
        tool_subset=set(tools or {"read_task_progress"}),
    )


def test_v5_upgrade_preserves_user_data_and_failure_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "user.sqlite3"
    repo = Repository(path)
    repo.initialize()
    AgentRegistry(repo).update_role_instruction("memory", "my memory rule", {"read_feedback"})
    task = repo.create_task(TaskCreate(title="existing work"))
    run = repo.create_run("existing request")
    with repo.transaction() as connection:
        connection.execute(
            "INSERT INTO memories(id,kind,value_json,status,created_at_utc,updated_at_utc) "
            "VALUES ('kept-memory','text_preference','{\"text\":\"later\"}',"
            "'approved','x','x')"
        )
    with sqlite3.connect(path) as connection:
        for table in (
            "custom_agent_usage", "custom_agent_trace_events",
            "custom_agent_proposals", "custom_agent_runs",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP TABLE custom_agents")
        connection.execute("PRAGMA user_version=5")

    original = migrations.migrate_v6

    def fail_after_create(connection):
        original(connection)
        raise RuntimeError("injected_v6_failure")

    monkeypatch.setattr(migrations, "migrate_v6", fail_after_create)
    with pytest.raises(RuntimeError, match="injected_v6_failure"):
        repo.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='custom_agents'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT title FROM tasks WHERE id=?", (task["id"],)).fetchone()[0] == "existing work"
    backups = list(tmp_path.glob("user.backup-v5-*.sqlite3"))
    assert backups
    monkeypatch.setattr(migrations, "migrate_v6", original)
    repo.initialize()
    assert repo.get_task(task["id"])["title"] == "existing work"
    assert repo.get_run(run["id"])["id"] == run["id"]
    assert repo.list_approved_memories()[0]["id"] == "kept-memory"
    assert repo.get_agent_config("memory")["instructions"] == "my memory rule"
    assert len(repo.list_agent_configs()) == 6
    with repo.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION


def test_custom_agent_permissions_versions_persistence_and_builtin_path(flow_system):
    repo, _, _, _, _, service = flow_system
    original_roles = service.list_roles()
    original_run_count = len(service.list_runs())
    with pytest.raises(PermissionError, match="read_only_maximum"):
        service.create_custom_agent(definition(tools={"commit_approved_plan"}))
    with pytest.raises(ValueError, match="name_reserved"):
        service.create_custom_agent(definition(name="Memory"))
    assert service.list_custom_agents() == []

    created = service.create_custom_agent(definition())
    assert created["status"] == "paused" and created["version"] == 1
    assert len(service.list_runs()) == original_run_count
    with pytest.raises(ValueError, match="duplicate_custom_agent_name"):
        service.create_custom_agent(definition(name="review HELPER"))
    updated = service.update_custom_agent(
        created["id"], 1, definition(name="Daily reviewer", tools={"read_goals"})
    )
    assert updated["version"] == 2 and updated["tool_subset"] == ["read_goals"]
    with pytest.raises(ValueError, match="stale_custom_agent_version"):
        service.update_custom_agent(created["id"], 1, definition())
    active = service.set_custom_agent_status(created["id"], 2, "active")
    assert active["version"] == 3 and active["status"] == "active"
    with pytest.raises(ValueError, match="stale_custom_agent_version"):
        service.set_custom_agent_status(created["id"], 2, "paused")
    paused = service.set_custom_agent_status(created["id"], 3, "paused")
    assert paused["status"] == "paused"

    reopened = Repository(repo.path)
    reopened.initialize()
    assert reopened.get_custom_agent(created["id"])["status"] == "paused"
    assert service.list_roles() == original_roles
    assert len(service.list_runs()) == original_run_count
    assert service.start_plan("明晚七点有 AI Agent 面试，请安排学习").tasks
    assert [step["agent"] for step in service.list_run_steps(service.list_runs()[0]["id"])] == [
        "orchestrator", "memory", "learning", "schedule", "task"
    ]
    events = [event["event_type"] for event in repo.list_trace()
              if event["event_type"].startswith("custom_agent_")]
    assert events == ["custom_agent_created", "custom_agent_updated",
                      "custom_agent_status_changed", "custom_agent_status_changed"]


def test_tampered_stored_permissions_cannot_be_enabled(flow_system):
    repo, _, _, _, _, service = flow_system
    agent = service.create_custom_agent(definition())
    with repo.transaction() as connection:
        connection.execute(
            "UPDATE custom_agents SET tool_subset_json=? WHERE id=?",
            (json.dumps(["commit_approved_plan"]), agent["id"]),
        )
    with pytest.raises(PermissionError, match="stored_custom_agent_permissions"):
        service.list_custom_agents()
    with pytest.raises(PermissionError, match="stored_custom_agent_permissions"):
        service.set_custom_agent_status(agent["id"], agent["version"], "active")
    assert repo.get_custom_agent(agent["id"])["status"] == "paused"
