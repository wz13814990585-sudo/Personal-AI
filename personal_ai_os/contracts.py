"""Validated application contracts shared by storage and later Agent code."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


AgentRole = Literal["orchestrator", "memory", "learning", "life", "schedule", "task"]
RunKind = Literal["planning", "feedback"]
RunStatus = Literal[
    "pending", "running", "waiting_approval", "success", "failed", "cancelled"
]
StepStatus = Literal["pending", "running", "success", "failed", "skipped"]
TaskStatus = Literal["pending", "in_progress", "completed"]
Priority = Literal["low", "medium", "high"]
BlockKind = Literal["available", "busy"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomAgentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(min_length=1, max_length=8000)
    tool_subset: set[str] = Field(default_factory=set)

    @field_validator("name", "description", "instructions")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def required_text(self) -> "CustomAgentCreate":
        if not self.name or not self.instructions:
            raise ValueError("custom_agent_text_required")
        return self


class CustomAgentProposalOutput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=8000)
    suggested_tools: list[str] = Field(default_factory=list, max_length=6)
    explanation: str = Field(min_length=1, max_length=2000)


class CustomAdviceItem(StrictModel):
    action: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)
    source_ids: list[str] = Field(default_factory=list, max_length=10)


class CustomAgentAdvice(StrictModel):
    summary: str = Field(min_length=1, max_length=2000)
    recommendations: list[CustomAdviceItem] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=8)


class CustomPlanningStep(StrictModel):
    """An explicitly selected, read-only step before the built-in planner."""

    step_id: str = Field(pattern=r"^custom_[A-Za-z0-9_-]{1,40}$")
    agent_id: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list, max_length=4)


class SuggestionStrategy(StrictModel):
    """User-reviewed thresholds for grounded proactive signals."""

    overdue_days: int = Field(ge=0, le=7)
    habit_gap_min: int = Field(ge=1, le=7)
    review_unfinished_min: int = Field(ge=1, le=20)


class StepSpec(StrictModel):
    step_id: str = Field(min_length=1)
    agent: AgentRole
    purpose: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class ExecutionPlan(StrictModel):
    intent: str = Field(min_length=1)
    steps: list[StepSpec] = Field(max_length=5)


class MemoryReference(StrictModel):
    memory_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class MemoryContext(StrictModel):
    memories: list[MemoryReference] = Field(default_factory=list)
    explanation: str = Field(min_length=1)


class LearningItem(StrictModel):
    item_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    estimated_minutes: int = Field(gt=0)
    priority: Priority
    reason: str = Field(min_length=1)
    due_at: AwareDatetime | None = None
    resource_video_id: str | None = None


class LearningPlan(StrictModel):
    items: list[LearningItem] = Field(default_factory=list, max_length=8)
    explanation: str = Field(min_length=1)


class LifeItem(StrictModel):
    item_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    estimated_minutes: int = Field(gt=0)
    priority: Priority
    reason: str = Field(min_length=1)
    due_at: AwareDatetime | None = None


class LifePlan(StrictModel):
    items: list[LifeItem] = Field(default_factory=list, max_length=8)
    explanation: str = Field(min_length=1)


class ScheduleAssignment(StrictModel):
    item_id: str = Field(min_length=1)
    start_at: AwareDatetime
    end_at: AwareDatetime
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_span(self) -> "ScheduleAssignment":
        _validate_span(self.start_at, self.end_at)
        return self


class ScheduleProposal(StrictModel):
    assignments: list[ScheduleAssignment] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1)


class TaskDraft(StrictModel):
    draft_item_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    estimated_minutes: int = Field(gt=0)
    priority: Priority
    domain: Literal["study", "life"] = "study"
    due_at: AwareDatetime | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    source_step_id: str = Field(min_length=1)
    resource_url: str | None = None
    resource_source: str | None = None
    resource_channel: str | None = None
    resource_duration_seconds: int | None = None

    @model_validator(mode="after")
    def valid_span(self) -> "TaskDraft":
        _validate_span(self.start_at, self.end_at)
        return self


class PlanDraft(StrictModel):
    run_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    tasks: list[TaskDraft] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)
    explanations: list[str] = Field(default_factory=list)


class DailyPlanAction(StrictModel):
    action_id: str = Field(min_length=1)
    kind: Literal["create_task", "reschedule_task"]
    task_id: str | None = None
    title: str = Field(min_length=1)
    domain: Literal["general", "study", "life"]
    priority: Priority
    estimated_minutes: int = Field(gt=0)
    due_at: AwareDatetime | None = None
    old_start_at: AwareDatetime | None = None
    old_end_at: AwareDatetime | None = None
    old_version: int | None = Field(default=None, ge=1)
    new_start_at: AwareDatetime | None = None
    new_end_at: AwareDatetime | None = None
    source: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    unassigned_reason: str | None = None

    @model_validator(mode="after")
    def valid_action(self) -> "DailyPlanAction":
        _validate_span(self.old_start_at, self.old_end_at)
        _validate_span(self.new_start_at, self.new_end_at)
        if self.kind == "create_task" and (
            self.task_id is not None or self.old_start_at is not None or self.old_version is not None
        ):
            raise ValueError("create_action_has_existing_task")
        if self.kind == "reschedule_task" and (not self.task_id or self.old_version is None):
            raise ValueError("reschedule_action_missing_task")
        return self


class DailyPlanDraft(StrictModel):
    id: str = Field(min_length=1)
    local_date: date
    timezone: str
    revision: int = Field(ge=0)
    baseline_fingerprint: str = Field(min_length=1)
    status: Literal["draft", "committed", "rejected"]
    actions: list[DailyPlanAction] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class TaskPlan(StrictModel):
    tasks: list[TaskDraft] = Field(default_factory=list)
    explanation: str = Field(min_length=1)


class MemoryProposal(StrictModel):
    feedback_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    value: dict[str, Any]
    source_excerpt: str
    explanation: str


class MemoryProposalBatch(StrictModel):
    proposals: list[MemoryProposal] = Field(default_factory=list, max_length=5)


class AgentResult(StrictModel):
    step_id: str = Field(min_length=1)
    status: Literal["success"] = "success"
    payload: ExecutionPlan | MemoryContext | LearningPlan | LifePlan | ScheduleProposal | TaskPlan | MemoryProposalBatch
    reason: str = Field(min_length=1)
    referenced_ids: list[str] = Field(default_factory=list)


class GoalCreate(StrictModel):
    title: str = Field(min_length=1)
    description: str = ""
    due_at: AwareDatetime | None = None


class TaskCreate(StrictModel):
    title: str = Field(min_length=1)
    goal_id: str | None = None
    priority: Priority = "medium"
    estimated_minutes: int = Field(default=30, gt=0)
    due_at: AwareDatetime | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_span(self) -> "TaskCreate":
        _validate_span(self.start_at, self.end_at)
        return self


class TimeBlockCreate(StrictModel):
    kind: BlockKind
    start_at: AwareDatetime
    end_at: AwareDatetime
    label: str = ""

    @model_validator(mode="after")
    def valid_span(self) -> "TimeBlockCreate":
        _validate_span(self.start_at, self.end_at)
        return self


class RecurrenceRuleCreate(StrictModel):
    title: str = Field(min_length=1)
    domain: Literal["general", "study", "life"] = "general"
    frequency: Literal["daily", "weekly_days"]
    weekdays: list[int] = Field(default_factory=list)
    timezone: str
    start_date: date
    end_date: date | None = None
    preferred_time: time | None = None
    estimated_minutes: int = Field(default=30, gt=0, le=1440)
    priority: Priority = "medium"

    @model_validator(mode="after")
    def valid_rule(self) -> "RecurrenceRuleCreate":
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid_timezone") from exc
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_before_start")
        if self.frequency == "daily" and self.weekdays:
            raise ValueError("daily_rule_has_weekdays")
        if self.frequency == "weekly_days" and (
            not self.weekdays or len(set(self.weekdays)) != len(self.weekdays)
            or any(day < 0 or day > 6 for day in self.weekdays)
        ):
            raise ValueError("invalid_weekdays")
        if self.preferred_time is not None and self.preferred_time.tzinfo is not None:
            raise ValueError("preferred_time_must_be_local")
        return self


class HabitCreate(StrictModel):
    title: str = Field(min_length=1)
    timezone: str
    target_per_week: int = Field(default=7, ge=1, le=7)

    @model_validator(mode="after")
    def valid_habit(self) -> "HabitCreate":
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid_timezone") from exc
        return self


class HabitCheckin(StrictModel):
    local_date: date
    completed: bool
    note: str = ""


def _validate_span(start: datetime | None, end: datetime | None) -> None:
    if (start is None) != (end is None):
        raise ValueError("start_at and end_at must be set together")
    if start is not None and end is not None and start >= end:
        raise ValueError("start_at must be earlier than end_at")
