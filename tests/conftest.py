from datetime import datetime, timezone

import pytest

from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.config import load_config
from personal_ai_os.contracts import (
    ExecutionPlan, LearningItem, LearningPlan, MemoryContext, MemoryProposal,
    MemoryProposalBatch, MemoryReference, ScheduleAssignment, ScheduleProposal,
    StepSpec, TaskDraft, TaskPlan, TimeBlockCreate,
)
from personal_ai_os.harness import Harness
from personal_ai_os.services import PersonalAIService
from personal_ai_os.storage import Repository
from personal_ai_os.tool_gateway import ToolGateway


def moment(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


class FlowRunner:
    """Deterministic Agent outputs; no fake trace is written by this runner."""

    def __init__(self):
        self.calls = []
        self.fail_feedback = False

    def run(self, role, payload, output_schema, tools):
        self.calls.append((role, output_schema.__name__, payload))
        if output_schema is MemoryProposalBatch:
            if self.fail_feedback:
                raise TimeoutError("injected feedback timeout")
            return MemoryProposalBatch(proposals=[MemoryProposal(
                feedback_id=payload["feedback_id"], kind="study_time_avoid",
                value={"start": "08:00", "end": "10:00"},
                source_excerpt="不要在早上安排学习",
                explanation="User dislikes morning study",
            )])
        if role == "orchestrator":
            return ExecutionPlan(intent="interview", steps=[
                StepSpec(step_id="memory", agent="memory", purpose="retrieve"),
                StepSpec(step_id="learning", agent="learning", purpose="break down", depends_on=["memory"]),
                StepSpec(step_id="schedule", agent="schedule", purpose="schedule", depends_on=["learning"]),
                StepSpec(step_id="task", agent="task", purpose="draft", depends_on=["schedule"]),
            ])
        if role == "memory":
            return MemoryContext(
                memories=[MemoryReference(memory_id=item["id"], reason="Relevant preference")
                          for item in payload["approved_memory_candidates"]],
                explanation="Selected approved memories",
            )
        if role == "learning":
            return LearningPlan(items=[LearningItem(
                item_id="review", title="Review AI Agents", estimated_minutes=60,
                priority="high", reason="Interview preparation", due_at=moment(11, 7),
            )], explanation="One learning task")
        if role == "schedule":
            avoid_morning = any(
                rule["start"] <= "09:00" < rule["end"]
                for rule in payload["hard_time_rules"]
            )
            start = moment(11, 4) if avoid_morning else moment(10, 22)
            end = moment(11, 5) if avoid_morning else moment(10, 23)
            return ScheduleProposal(assignments=[ScheduleAssignment(
                item_id="review", start_at=start, end_at=end, reason="Available window",
            )], explanation="Scheduled by preference")
        assert "commit_approved_plan" not in tools.available_names
        return TaskPlan(tasks=[TaskDraft(
            draft_item_id="review", title="Review AI Agents", estimated_minutes=60,
            priority="high", due_at=moment(11, 7), source_step_id="learning",
        )], explanation="Draft only")


@pytest.fixture
def flow_system(tmp_path):
    repository = Repository(tmp_path / "flow.sqlite3")
    repository.initialize()
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=moment(10, 22), end_at=moment(10, 23)
    ))
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=moment(11, 4), end_at=moment(11, 5)
    ))
    registry = AgentRegistry(repository)
    config = load_config(environ={})
    gateway = ToolGateway(repository, registry, config.timezone)
    runner = FlowRunner()
    harness = Harness(repository, registry, gateway, runner, config)
    service = PersonalAIService(repository, gateway, harness)
    return repository, registry, gateway, runner, harness, service
