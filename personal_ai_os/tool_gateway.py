"""Validated, role-bound access to local application tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from .agent_registry import AgentRegistry
from .contracts import StrictModel
from .memory import search_approved
from .storage import Repository


class NoArgs(StrictModel):
    pass


class StatusArgs(StrictModel):
    status: str | None = None


class FeedbackArgs(StrictModel):
    feedback_id: str | None = None


class MemorySearchArgs(StrictModel):
    query: str = ""
    limit: int = Field(default=5, ge=1, le=20)


class CommitPlanArgs(StrictModel):
    run_id: str = Field(min_length=1)
    revision: int = Field(ge=0)


@dataclass(frozen=True)
class ApprovalContext:
    run_id: str
    revision: int
    _seal: object


class ItemsResult(StrictModel):
    items: list[dict[str, Any]]


class SettingsResult(StrictModel):
    settings: dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    handler: Callable[[BaseModel], BaseModel]


class BoundTools:
    """A model receives this captured role, never a model-selected role."""

    def __init__(
        self, gateway: "ToolGateway", role: str, run_id: str | None,
        step_id: str | None = None, max_calls: int = 4,
    ):
        self._gateway = gateway
        self._role = role
        self._run_id = run_id
        self._step_id = step_id
        self._max_calls = max_calls
        self._calls = 0
        self.failure_code: str | None = None

    def invoke(self, tool_name: str, args: dict[str, Any]) -> BaseModel:
        try:
            if self._calls >= self._max_calls:
                self._gateway._deny(self._role, tool_name, "tool_call_limit", self._run_id, self._step_id)
            self._calls += 1
            return self._gateway.invoke(
                self._role, tool_name, args, run_id=self._run_id, step_id=self._step_id
            )
        except PermissionError:
            self.failure_code = "permission_denied"
            raise
        except Exception:
            self.failure_code = "tool_error"
            raise

    @property
    def available_names(self) -> list[str]:
        return [
            name for name in self._gateway.registry.get_role(self._role)["tool_subset"]
            if name in self._gateway._specs
        ]


class ToolGateway:
    def __init__(
        self, repository: Repository, registry: AgentRegistry,
        timezone_name: str = "Australia/Sydney",
    ):
        self.repository = repository
        self.registry = registry
        self.timezone_name = timezone_name
        self._approval_seal = object()
        self._specs: dict[str, ToolSpec] = {
            "list_agent_capabilities": ToolSpec(
                NoArgs, ItemsResult,
                lambda _: ItemsResult(items=registry.list_roles()),
            ),
            "read_goal_summary": ToolSpec(
                NoArgs, ItemsResult,
                lambda _: ItemsResult(items=repository.list_goals()),
            ),
            "search_approved_memories": ToolSpec(
                MemorySearchArgs, ItemsResult, self._search_memories,
            ),
            "read_feedback": ToolSpec(
                FeedbackArgs, ItemsResult, self._read_feedback,
            ),
            "read_goals": ToolSpec(
                NoArgs, ItemsResult,
                lambda _: ItemsResult(items=repository.list_goals()),
            ),
            "read_task_progress": ToolSpec(
                NoArgs, ItemsResult,
                lambda _: ItemsResult(items=repository.list_tasks()),
            ),
            "read_time_blocks": ToolSpec(
                NoArgs, ItemsResult,
                lambda _: ItemsResult(items=repository.list_time_blocks()),
            ),
            "read_scheduled_tasks": ToolSpec(
                NoArgs, ItemsResult,
                lambda _: ItemsResult(items=[
                    row for row in repository.list_tasks()
                    if row["start_at_utc"] is not None and row["status"] != "completed"
                ]),
            ),
            "read_settings": ToolSpec(
                NoArgs, SettingsResult,
                lambda _: SettingsResult(settings=repository.list_settings()),
            ),
            "read_tasks": ToolSpec(
                StatusArgs, ItemsResult, self._read_tasks,
            ),
        }

    def issue_approval(self, run_id: str, revision: int) -> ApprovalContext:
        """Called only by the user-facing service after an explicit confirm action."""
        return ApprovalContext(run_id, revision, self._approval_seal)

    def bind(
        self, role: str, run_id: str | None = None,
        step_id: str | None = None, max_calls: int = 4,
    ) -> BoundTools:
        if role not in {item["role"] for item in self.registry.list_roles()}:
            raise ValueError("Unknown Agent role")
        return BoundTools(self, role, run_id, step_id, max_calls)

    def invoke(
        self,
        bound_role: str,
        tool_name: str,
        args: dict[str, Any],
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        approval_context: Any = None,
    ) -> BaseModel:
        if tool_name == "commit_approved_plan":
            try:
                validated_commit = CommitPlanArgs.model_validate(args)
            except ValidationError:
                self._deny(bound_role, tool_name, "invalid_arguments", run_id, step_id)
            if not self.registry.allows(bound_role, tool_name):
                self._deny(bound_role, tool_name, "role_not_allowed", run_id, step_id)
            if (
                not isinstance(approval_context, ApprovalContext)
                or approval_context._seal is not self._approval_seal
                or approval_context.run_id != validated_commit.run_id
                or approval_context.revision != validated_commit.revision
                or run_id != validated_commit.run_id
            ):
                self._deny(bound_role, tool_name, "approval_required", run_id, step_id)
            try:
                tasks = self.repository.commit_approved_plan(
                    validated_commit.run_id, validated_commit.revision, self.timezone_name
                )
            except Exception as exc:
                self.repository.append_trace(
                    "tool_failed", {"tool": tool_name, "error_type": type(exc).__name__},
                    run_id=run_id, step_id=step_id, actor="system",
                )
                raise
            self.repository.append_trace(
                "tool_called", {"tool": tool_name, "task_count": len(tasks)},
                run_id=run_id, step_id=step_id, actor="system",
            )
            return ItemsResult(items=tasks)

        spec = self._specs.get(tool_name)
        if spec is None:
            self._deny(bound_role, tool_name, "unknown_tool", run_id, step_id)
        try:
            validated = spec.input_schema.model_validate(args)
        except ValidationError:
            self._deny(bound_role, tool_name, "invalid_arguments", run_id, step_id)
        if not self.registry.allows(bound_role, tool_name):
            self._deny(bound_role, tool_name, "role_not_allowed", run_id, step_id)
        try:
            result = spec.handler(validated)
            checked = spec.output_schema.model_validate(result)
        except Exception as exc:
            self.repository.append_trace(
                "tool_failed", {"tool": tool_name, "error_type": type(exc).__name__},
                run_id=run_id, step_id=step_id, actor="agent",
            )
            raise
        self.repository.append_trace(
            "tool_called", {"tool": tool_name, "arg_keys": sorted(args)},
            run_id=run_id, step_id=step_id, actor="agent",
        )
        return checked

    def _deny(
        self, role: str, tool: str, reason: str,
        run_id: str | None, step_id: str | None = None,
    ) -> None:
        self.repository.append_trace(
            "tool_denied", {"role": role, "tool": tool, "reason": reason},
            run_id=run_id, step_id=step_id, actor="agent",
        )
        raise PermissionError(f"Tool denied: {reason}")

    def _search_memories(self, args: MemorySearchArgs) -> ItemsResult:
        return ItemsResult(items=search_approved(
            self.repository.list_approved_memories(), args.query, args.limit
        ))

    def _read_feedback(self, args: FeedbackArgs) -> ItemsResult:
        rows = self.repository.list_feedback()
        if args.feedback_id:
            rows = [row for row in rows if row["id"] == args.feedback_id]
        return ItemsResult(items=rows)

    def _read_tasks(self, args: StatusArgs) -> ItemsResult:
        return ItemsResult(items=self.repository.list_tasks(status=args.status))
