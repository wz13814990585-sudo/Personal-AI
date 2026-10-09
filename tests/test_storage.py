from datetime import datetime, timezone

import pytest

from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.contracts import GoalCreate, TaskCreate, TimeBlockCreate
from personal_ai_os.storage import Repository


def dt(hour: int) -> datetime:
    return datetime(2026, 10, 9, hour, tzinfo=timezone.utc)


def test_data_and_agent_configuration_survive_reopen(tmp_path):
    path = tmp_path / "nested" / "personal.sqlite3"
    first = Repository(path)
    first.initialize()
    AgentRegistry(first)
    first.set_setting("timezone", "Australia/Sydney")
    goal = first.create_goal(GoalCreate(title="准备面试", due_at=dt(20)))
    available = first.create_time_block(
        TimeBlockCreate(kind="available", start_at=dt(9), end_at=dt(18))
    )
    task = first.create_task(
        TaskCreate(title="复习 Agent 基础", goal_id=goal["id"], start_at=dt(10), end_at=dt(11))
    )
    first.set_task_status(task["id"], "completed")

    reopened = Repository(path)
    reopened.initialize()
    assert reopened.get_setting("timezone") == "Australia/Sydney"
    assert reopened.get_goal(goal["id"])["title"] == "准备面试"
    assert reopened.get_task(task["id"])["status"] == "completed"
    assert reopened.get_time_block(available["id"])["kind"] == "available"
    assert {row["role"] for row in reopened.list_agent_configs()} == {
        "orchestrator", "memory", "learning", "life", "schedule", "task"
    }
    with reopened.connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "settings", "goals", "tasks", "time_blocks", "agent_configs", "feedback",
        "memory_proposals", "memories", "runs", "run_steps", "trace_events",
    } <= tables


def test_crud_and_transaction_rollback(tmp_path):
    repository = Repository(tmp_path / "db.sqlite3")
    repository.initialize()
    goal = repository.create_goal(GoalCreate(title="原目标"))
    repository.update_goal(goal["id"], GoalCreate(title="新目标"))
    assert repository.get_goal(goal["id"])["title"] == "新目标"
    block = repository.create_time_block(
        TimeBlockCreate(kind="available", start_at=dt(9), end_at=dt(18))
    )
    task = repository.create_task(TaskCreate(title="初始任务"))
    assert task["start_at_utc"] is None
    repository.update_task(
        task["id"], TaskCreate(title="已安排任务", start_at=dt(12), end_at=dt(13))
    )
    assert repository.get_task(task["id"])["start_at_utc"] == dt(12).isoformat()
    repository.update_time_block(
        block["id"], TimeBlockCreate(kind="available", start_at=dt(8), end_at=dt(19))
    )

    with pytest.raises(RuntimeError):
        with repository.transaction() as connection:
            connection.execute("INSERT INTO settings(key,value_json) VALUES ('temporary','true')")
            raise RuntimeError("rollback")
    assert repository.get_setting("temporary") is None

    repository.delete_task(task["id"])
    repository.delete_time_block(block["id"])
    repository.delete_goal(goal["id"])
    assert repository.list_tasks() == []
    assert repository.list_time_blocks() == []
    assert repository.list_goals() == []
