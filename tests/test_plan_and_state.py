import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.config import load_config
from personal_ai_os.contracts import (
    ExecutionPlan, LearningItem, LearningPlan, MemoryContext, ScheduleAssignment,
    ScheduleProposal, StepSpec, TaskDraft, TaskPlan, TimeBlockCreate,
)
from personal_ai_os.harness import Harness
from personal_ai_os.model_factory import make_deepseek_model
from personal_ai_os.orchestrator import validate_execution_plan
from personal_ai_os.storage import Repository
from personal_ai_os.tool_gateway import ToolGateway


def at(hour: int) -> datetime:
    return datetime(2026, 10, 10, hour, tzinfo=timezone.utc)


def full_plan() -> ExecutionPlan:
    return ExecutionPlan(
        intent="prepare interview",
        steps=[
            StepSpec(step_id="task", agent="task", purpose="draft tasks", depends_on=["schedule"]),
            StepSpec(step_id="schedule", agent="schedule", purpose="place tasks", depends_on=["learning"]),
            StepSpec(step_id="learning", agent="learning", purpose="study plan", depends_on=["memory"]),
            StepSpec(step_id="memory", agent="memory", purpose="check preferences"),
        ],
    )


class FakeRunner:
    def __init__(self, *, fail_role=None, use_forbidden_tool=False):
        self.calls = []
        self.fail_role = fail_role
        self.use_forbidden_tool = use_forbidden_tool

    def run(self, role, payload, output_schema, tools):
        self.calls.append((role, payload))
        if role == self.fail_role:
            raise TimeoutError("injected timeout")
        if role == "orchestrator":
            return full_plan()
        if role == "memory":
            if self.use_forbidden_tool:
                tools.invoke("commit_approved_plan", {"run_id": "fake", "revision": 0})
            assert tools.invoke("search_approved_memories", {}).items == []
            return MemoryContext(memories=[], explanation="No confirmed preference")
        if role == "learning":
            assert payload["selected_memories"] == []
            return LearningPlan(
                items=[LearningItem(
                    item_id="review", title="Review Agent concepts", estimated_minutes=60,
                    priority="high", reason="Interview preparation", due_at=at(19),
                )],
                explanation="One focused preparation task",
            )
        if role == "schedule":
            assert payload["learning_plan"]["items"][0]["item_id"] == "review"
            return ScheduleProposal(
                assignments=[ScheduleAssignment(
                    item_id="review", start_at=at(10), end_at=at(11),
                    reason="Inside available block",
                )], explanation="Placed before interview",
            )
        assert "commit_approved_plan" not in tools.available_names
        return TaskPlan(
            tasks=[TaskDraft(
                draft_item_id="review", title="Review Agent concepts",
                estimated_minutes=60, priority="high", due_at=at(19),
                source_step_id="learning",
            )],
            explanation="Draft only",
        )


def setup(tmp_path, runner):
    repository = Repository(tmp_path / "db.sqlite3")
    repository.initialize()
    repository.create_time_block(
        TimeBlockCreate(kind="available", start_at=at(9), end_at=at(18))
    )
    registry = AgentRegistry(repository)
    config = load_config(environ={})
    harness = Harness(repository, registry, ToolGateway(repository, registry), runner, config)
    run = repository.create_run("明晚七点有 AI Agent 面试，请制定学习计划并安排任务")
    return repository, harness, run["id"]


def test_five_agents_produce_persistent_draft_and_trace_without_task_write(tmp_path):
    runner = FakeRunner()
    repository, harness, run_id = setup(tmp_path, runner)
    draft = harness.execute_plan(run_id)
    assert [role for role, _ in runner.calls] == [
        "orchestrator", "memory", "learning", "schedule", "task"
    ]
    schedule_input = next(payload for role, payload in runner.calls if role == "schedule")
    assert schedule_input["allowed_item_ids"] == ["review"]
    assert schedule_input["available_local_windows"] == [{
        "start_at": "2026-10-10T20:00:00+11:00",
        "end_at": "2026-10-11T05:00:00+11:00",
    }]
    assert draft.tasks[0].start_at == at(10)
    assert draft.conflicts == []
    assert repository.list_tasks() == []
    assert repository.get_run(run_id)["status"] == "waiting_approval"
    snapshot = json.loads(repository.get_run(run_id)["config_snapshot_json"])
    assert snapshot["model_id"] == "deepseek-flash"
    assert set(snapshot["agent_versions"]) == {
        "orchestrator", "memory", "learning", "life", "schedule", "task"
    }
    assert [step["status"] for step in repository.list_run_steps(run_id)] == ["success"] * 5
    assert [step["agent"] for step in repository.list_run_steps(run_id)] == [
        "orchestrator", "memory", "learning", "schedule", "task"
    ]
    trace = repository.list_trace(run_id)
    assert [event["event_type"] for event in trace].count("delegated") == 4
    assert any(event["event_type"] == "tool_called" and event["step_id"] == "memory" for event in trace)
    reopened = Repository(repository.path)
    saved = json.loads(reopened.get_run(run_id)["draft_json"])
    assert saved["tasks"][0]["title"] == "Review Agent concepts"
    assert reopened.list_tasks() == []


def test_failed_model_step_skips_dependents_and_writes_no_tasks(tmp_path):
    repository, harness, run_id = setup(tmp_path, FakeRunner(fail_role="learning"))
    with pytest.raises(TimeoutError):
        harness.execute_plan(run_id)
    assert repository.get_run(run_id)["status"] == "failed"
    assert [step["status"] for step in repository.list_run_steps(run_id)] == [
        "success", "success", "failed", "skipped", "skipped"
    ]
    assert repository.list_tasks() == []
    assert any(event["event_type"] == "run_failed" for event in repository.list_trace(run_id))


def test_forbidden_model_tool_fails_memory_step_and_records_bound_identity(tmp_path):
    repository, harness, run_id = setup(tmp_path, FakeRunner(use_forbidden_tool=True))
    with pytest.raises(PermissionError, match="role_not_allowed"):
        harness.execute_plan(run_id)
    assert repository.list_tasks() == []
    assert repository.get_run(run_id)["status"] == "failed"
    denied = [event for event in repository.list_trace(run_id) if event["event_type"] == "tool_denied"]
    assert len(denied) == 1
    assert denied[0]["step_id"] == "memory"
    assert json.loads(denied[0]["summary_json"])["role"] == "memory"


def test_invalid_model_output_fails_without_partial_task_write(tmp_path):
    class InvalidRunner(FakeRunner):
        def run(self, role, payload, output_schema, tools):
            if role == "learning":
                return "not structured output"
            return super().run(role, payload, output_schema, tools)

    repository, harness, run_id = setup(tmp_path, InvalidRunner())
    with pytest.raises(ValueError, match="invalid_model_output"):
        harness.execute_plan(run_id)
    assert repository.get_run(run_id)["status"] == "failed"
    assert repository.list_run_steps(run_id)[2]["error_code"] == "invalid_output"
    assert repository.list_tasks() == []


def test_tool_denial_still_fails_step_if_runner_swallows_exception(tmp_path):
    class SwallowingRunner(FakeRunner):
        def run(self, role, payload, output_schema, tools):
            if role == "memory":
                try:
                    tools.invoke("commit_approved_plan", {"run_id": "fake", "revision": 0})
                except PermissionError:
                    pass
                return MemoryContext(memories=[], explanation="Ignored the denial")
            return super().run(role, payload, output_schema, tools)

    repository, harness, run_id = setup(tmp_path, SwallowingRunner())
    with pytest.raises(PermissionError, match="tool_denied_during_step"):
        harness.execute_plan(run_id)
    assert repository.list_run_steps(run_id)[1]["status"] == "failed"
    assert repository.list_tasks() == []


def test_conflicting_schedule_stays_unassigned_in_draft(tmp_path):
    class ConflictRunner(FakeRunner):
        def run(self, role, payload, output_schema, tools):
            if role == "schedule":
                return ScheduleProposal(
                    assignments=[ScheduleAssignment(
                        item_id="review", start_at=at(18), end_at=at(19),
                        reason="bad suggestion",
                    )], explanation="suggestion requires validation",
                )
            return super().run(role, payload, output_schema, tools)

    repository, harness, run_id = setup(tmp_path, ConflictRunner())
    draft = harness.execute_plan(run_id)
    assert draft.tasks[0].start_at is None
    assert any("outside_availability" in issue for issue in draft.conflicts)
    assert repository.list_tasks() == []


def test_invalid_graph_and_interrupted_run_are_detected(tmp_path):
    with pytest.raises(ValueError, match="cyclic"):
        validate_execution_plan(ExecutionPlan(
            intent="cycle", steps=[
                StepSpec(step_id="memory", agent="memory", purpose="a", depends_on=["task"]),
                StepSpec(step_id="task", agent="task", purpose="b", depends_on=["memory"]),
            ],
        ))
    repository = Repository(tmp_path / "interrupted.sqlite3")
    repository.initialize()
    run_id = repository.create_run("test")["id"]
    repository.update_run_status(run_id, "running")
    repository.create_run_step(run_id, "memory", "memory", [])
    repository.update_run_step(run_id, "memory", "running")
    assert repository.fail_interrupted_runs() == 1
    assert repository.get_run(run_id)["status"] == "failed"
    assert repository.list_run_steps(run_id)[0]["error_code"] == "interrupted"


def test_complex_request_rejects_missing_roles_and_tool_call_limit(tmp_path):
    class MissingRoleRunner(FakeRunner):
        def run(self, role, payload, output_schema, tools):
            if role == "orchestrator":
                return ExecutionPlan(
                    intent="study", steps=[StepSpec(
                        step_id="task", agent="task", purpose="draft only"
                    )],
                )
            return super().run(role, payload, output_schema, tools)

    repository, harness, run_id = setup(tmp_path, MissingRoleRunner())
    with pytest.raises(ValueError, match="missing_required_worker_role"):
        harness.execute_plan(run_id)
    assert repository.get_run(run_id)["status"] == "failed"
    assert repository.list_run_steps(run_id)[0]["status"] == "failed"

    registry = AgentRegistry(repository)
    bound = ToolGateway(repository, registry).bind("memory", max_calls=1)
    bound.invoke("search_approved_memories", {})
    with pytest.raises(PermissionError, match="tool_call_limit"):
        bound.invoke("search_approved_memories", {})
    assert json.loads(repository.list_trace()[-1]["summary_json"])["reason"] == "tool_call_limit"


def test_model_factory_rejects_non_deepseek_even_with_manual_config():
    config = load_config(require_model=True, environ={"DEEPSEEK_API_KEY": "test-key"})
    assert make_deepseek_model(config).id == "deepseek-flash"
    with pytest.raises(ValueError, match="DeepSeek"):
        make_deepseek_model(replace(config, deepseek_model_id="gpt-4o"))
