"""Reviewed proposals, read-only execution, failures and durable traces."""

import sqlite3
from dataclasses import replace

import pytest

from personal_ai_os import migrations
from personal_ai_os.contracts import (
    CustomAdviceItem, CustomAgentAdvice, CustomAgentCreate,
    CustomAgentProposalOutput, GoalCreate,
)
from personal_ai_os.custom_agent_store import CustomAgentStore
from personal_ai_os.services import PersonalAIService
from personal_ai_os.storage import Repository


def definition(name="Study reviewer", tools=None):
    return CustomAgentCreate(
        name=name, description="Review current goals", instructions="Only read and advise.",
        tool_subset=set(tools or {"read_goals"}),
    )


class FakeCustomRunner:
    def __init__(self, config=None):
        self.mode = "normal"
        self.calls = []
        self._usage = []

    def _request(self):
        self._usage = [{
            "model_id": "deepseek-flash", "input_tokens": 20,
            "output_tokens": 10, "total_tokens": 30, "duration_ms": 12,
        }]

    def consume_usage(self):
        result, self._usage = self._usage, []
        return result

    def propose(self, goal, allowed_tools, existing_names):
        self.calls.append(("proposal", goal, allowed_tools, existing_names))
        self._request()
        return CustomAgentProposalOutput(
            name="Study reviewer", description="Review current goals",
            instructions="Only read goals and advise.",
            suggested_tools=["commit_approved_plan"] if self.mode == "unsafe_proposal" else ["read_goals"],
            explanation="The user asked for a goal review.",
        )

    def advise(self, agent, request, tools):
        self.calls.append(("advice", agent["id"], request, tools.available_names))
        self._request()
        if self.mode == "forbidden_tool":
            try:
                tools.invoke("commit_approved_plan", {"run_id": "fake", "revision": 0})
            except PermissionError:
                pass
        elif self.mode == "timeout":
            raise TimeoutError("injected timeout")
        elif self.mode == "invalid_output":
            return {"summary": "not validated"}
        elif self.mode == "hallucinated_source":
            return CustomAgentAdvice(
                summary="Invented source", recommendations=[CustomAdviceItem(
                    action="Review goal", reason="Claimed evidence", source_ids=["not-read"]
                )],
            )
        else:
            goals = tools.invoke("read_goals", {}).items
            if self.mode == "pause_during_run":
                self.pause()
            return CustomAgentAdvice(
                summary="Review the user's goal",
                recommendations=[CustomAdviceItem(
                    action="Read for 30 minutes", reason="Aligned with saved goal",
                    source_ids=[goals[0]["id"]] if goals else [],
                )],
            )
        return CustomAgentAdvice(summary="Should be rejected")


def service_with_fake(flow_system):
    repo, registry, gateway, _, harness, _ = flow_system
    harness.config = replace(harness.config, deepseek_api_key="fake-key")
    fake = FakeCustomRunner()
    service = PersonalAIService(repo, gateway, harness, registry, custom_runner=fake)
    return repo, service, fake


def test_proposal_requires_review_then_manual_read_only_run(flow_system):
    repo, service, fake = service_with_fake(flow_system)
    goal = service.create_goal(GoalCreate(title="Learn Agent systems"))
    before_tasks = repo.list_tasks()
    before_memories = repo.list_approved_memories()
    before_blocks = repo.list_time_blocks()
    proposal = service.generate_custom_agent_proposal("Review my study goals")
    assert proposal["status"] == "pending" and proposal["suggested_tools"] == ["read_goals"]
    assert service.list_custom_agents() == []
    proposal_run = service.get_custom_agent_run(proposal["source_run_id"])
    assert proposal_run["status"] == "success" and proposal_run["kind"] == "proposal"
    assert service.list_custom_agent_usage(proposal_run["id"])[0]["total_tokens"] == 30
    with pytest.raises(ValueError, match="stale_custom_agent_proposal"):
        service.accept_custom_agent_proposal(proposal["id"], 3, definition())

    saved = service.accept_custom_agent_proposal(
        proposal["id"], proposal["revision"], definition(name="My study reviewer")
    )
    assert saved["status"] == "paused" and saved["tool_subset"] == ["read_goals"]
    assert service.list_custom_agent_proposals("pending") == []
    with pytest.raises(ValueError, match="stale_custom_agent_proposal"):
        service.accept_custom_agent_proposal(proposal["id"], proposal["revision"], definition())
    with pytest.raises(PermissionError, match="custom_agent_paused"):
        service.run_custom_agent(saved["id"], "Review my goals")
    assert len(service.list_custom_agent_runs()) == 1

    active = service.set_custom_agent_status(saved["id"], saved["version"], "active")
    run = service.run_custom_agent(active["id"], "Read my goals and advise")
    assert run["status"] == "success" and run["agent_version"] == active["version"]
    assert run["output"]["recommendations"][0]["source_ids"] == [goal["id"]]
    assert [event["event_type"] for event in service.list_custom_agent_trace(run["id"])] == [
        "run_started", "tool_called", "run_succeeded"
    ]
    assert service.list_custom_agent_usage(run["id"])[0]["model_id"] == "deepseek-flash"
    assert service.usage_summary()["request_count"] == 2
    assert repo.list_tasks() == before_tasks
    assert repo.list_approved_memories() == before_memories
    assert repo.list_time_blocks() == before_blocks
    assert repo.get_custom_agent(active["id"])["version"] == active["version"]
    assert len(service.list_roles()) == 6
    assert fake.calls[-1][3] == ["read_goals"]
    reopened = CustomAgentStore(Repository(repo.path))
    assert reopened.get_run(run["id"])["output"] == run["output"]
    assert reopened.get_proposal(proposal["id"])["status"] == "accepted"


def test_rejected_or_unsafe_proposal_never_creates_agent(flow_system):
    repo, service, fake = service_with_fake(flow_system)
    proposal = service.generate_custom_agent_proposal("Review my goals")
    rejected = service.reject_custom_agent_proposal(proposal["id"], proposal["revision"])
    assert rejected["status"] == "rejected" and service.list_custom_agents() == []
    with pytest.raises(ValueError, match="stale_custom_agent_proposal"):
        service.accept_custom_agent_proposal(proposal["id"], 0, definition())
    fake.mode = "unsafe_proposal"
    with pytest.raises(PermissionError, match="read_only_maximum"):
        service.generate_custom_agent_proposal("Do anything")
    failed = service.list_custom_agent_runs()[0]
    assert failed["status"] == "failed" and failed["error_code"] == "permission_denied"
    assert service.list_custom_agent_proposals("pending") == []
    assert repo.list_custom_agents() == []
    assert service.list_custom_agent_usage(failed["id"])[0]["error_code"] == "permission_denied"


@pytest.mark.parametrize("mode,expected", [
    ("forbidden_tool", "permission_denied"),
    ("timeout", "timeout"),
    ("invalid_output", "invalid_output"),
    ("hallucinated_source", "invalid_output"),
    ("pause_during_run", "permission_denied"),
])
def test_manual_run_failure_is_traced_without_business_write(flow_system, mode, expected):
    repo, service, fake = service_with_fake(flow_system)
    agent = service.create_custom_agent(definition())
    active = service.set_custom_agent_status(agent["id"], agent["version"], "active")
    before_tasks = repo.list_tasks()
    before_memories = repo.list_approved_memories()
    before_blocks = repo.list_time_blocks()
    fake.mode = mode
    if mode == "pause_during_run":
        fake.pause = lambda: service.set_custom_agent_status(
            active["id"], active["version"], "paused"
        )
    with pytest.raises((PermissionError, TimeoutError, ValueError)):
        service.run_custom_agent(active["id"], "Review goals")
    run = service.list_custom_agent_runs()[0]
    assert run["status"] == "failed" and run["error_code"] == expected
    assert service.list_custom_agent_usage(run["id"])[0]["error_code"] == expected
    events = [item["event_type"] for item in service.list_custom_agent_trace(run["id"])]
    assert events[0] == "run_started" and events[-1] == "run_failed"
    if mode == "forbidden_tool":
        assert "tool_denied" in events
    assert repo.list_tasks() == before_tasks
    assert repo.list_approved_memories() == before_memories
    assert repo.list_time_blocks() == before_blocks


def test_v6_upgrade_backup_rollback_and_interrupted_run(tmp_path, monkeypatch):
    path = tmp_path / "custom.sqlite3"
    repo = Repository(path)
    repo.initialize()
    agent = repo.create_custom_agent(definition())
    with sqlite3.connect(path) as connection:
        for table in (
            "custom_agent_usage", "custom_agent_trace_events",
            "custom_agent_proposals", "custom_agent_runs",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=6")
    original = migrations.migrate_v7

    def fail_after_ddl(connection):
        original(connection)
        raise RuntimeError("injected_v7_failure")

    monkeypatch.setattr(migrations, "migrate_v7", fail_after_ddl)
    with pytest.raises(RuntimeError, match="injected_v7_failure"):
        repo.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='custom_agent_runs'"
        ).fetchone()[0] == 0
    assert list(tmp_path.glob("custom.backup-v6-*.sqlite3"))
    monkeypatch.setattr(migrations, "migrate_v7", original)
    repo.initialize()
    assert repo.get_custom_agent(agent["id"])["name"] == "Study reviewer"
    store = CustomAgentStore(repo)
    interrupted = store.create_run("proposal", "Review goals", "deepseek-flash")
    assert store.fail_interrupted_runs() == 1
    assert store.get_run(interrupted["id"])["error_code"] == "interrupted"
    assert [event["event_type"] for event in store.list_events(interrupted["id"])] == [
        "run_started", "run_failed"
    ]
