"""Sequential planning runtime. A draft is never a committed task."""

from __future__ import annotations

import time
import hashlib
import json
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from agno.exceptions import ModelProviderError
from openai import APITimeoutError

from .agent_registry import AgentRegistry
from .agents import AgentRunner
from .config import AppConfig
from .conversation import recent_plan_history
from .contracts import (
    AgentResult, AgentRole, ExecutionPlan, LearningPlan, LifePlan, MemoryContext,
    MemoryProposalBatch, PlanDraft,
    ScheduleProposal, StepSpec, TaskDraft, TaskPlan,
)
from .memory import approved_study_avoid_ranges, normalize_proposal, search_approved
from .orchestrator import required_worker_roles, validate_execution_plan
from .schedule_rules import Interval, validate_schedule
from .storage import Repository
from .tool_gateway import ToolGateway


WORKER_SCHEMAS: dict[str, type[BaseModel]] = {
    "memory": MemoryContext,
    "learning": LearningPlan,
    "life": LifePlan,
    "schedule": ScheduleProposal,
    "task": TaskPlan,
}


class Harness:
    def __init__(
        self, repository: Repository, registry: AgentRegistry,
        gateway: ToolGateway, runner: AgentRunner, config: AppConfig,
    ):
        self.repository = repository
        self.registry = registry
        self.gateway = gateway
        self.runner = runner
        self.config = config

    def execute_plan(
        self, run_id: str, *, custom_context: list[dict[str, Any]] | None = None,
        youtube_resources: list[dict[str, Any]] | None = None,
    ) -> PlanDraft:
        run = self.repository.get_run(run_id)
        selected_steps = [step for step in self.repository.list_run_steps(run_id)
                          if step["step_id"].startswith("custom_")]
        custom_ready = (
            bool(custom_context) and len(selected_steps) == len(custom_context)
            and all(
                step["status"] == "success"
                and step["step_id"] == item["step_id"]
                and step["agent"] == item["agent_id"]
                and json.loads(step["result_json"])["advice"] == item["advice"]
                for step, item in zip(selected_steps, custom_context)
            )
            and any(event["event_type"] == "custom_graph_completed"
                    for event in self.repository.list_trace(run_id))
        )
        if run["kind"] != "planning" or not (
            (run["status"] == "pending" and not custom_context)
            or (run["status"] == "running" and custom_ready)
        ):
            raise ValueError("run_not_pending_planning")
        started = time.monotonic()
        self.repository.set_run_config_snapshot(run_id, {
            **json.loads(run["config_snapshot_json"]),
            "model_id": self.config.deepseek_model_id,
            "timezone": self.config.timezone,
            "agent_versions": {
                role["role"]: role["version"] for role in self.registry.list_roles()
            },
            "selected_custom_steps": [
                {"step_id": item["step_id"], "agent_id": item["agent_id"]}
                for item in custom_context or []
            ],
        })
        if run["status"] == "pending":
            self.repository.update_run_status(run_id, "running")
            self.repository.append_trace("run_started", {"kind": "planning"}, run_id=run_id)
        else:
            self.repository.append_trace("builtin_planning_started", {}, run_id=run_id)
        ordered: list[StepSpec] = []
        completed: dict[str, BaseModel] = {}
        current_step = "orchestrator"
        excluded_history_ids = {run_id}
        ancestor_id = run.get("retry_of_run_id")
        while ancestor_id and ancestor_id not in excluded_history_ids:
            excluded_history_ids.add(ancestor_id)
            ancestor_id = self.repository.get_run(ancestor_id).get("retry_of_run_id")
        try:
            self.repository.create_run_step(run_id, current_step, "orchestrator", [])
            plan = self._run_step(
                run_id, current_step, "orchestrator",
                {
                    "request": run["request_text"],
                    "current_local_time": self._local_now(),
                    "timezone": self.config.timezone,
                    "goals": self.repository.list_goals(),
                    "recent_conversation": recent_plan_history(
                        [item for item in self.repository.list_runs()
                         if item["id"] not in excluded_history_ids], run["conversation_id"],
                    ),
                    "agent_capabilities": [
                        {"role": role["role"], "description": role["description"]}
                        for role in self.registry.list_roles()
                    ],
                    "required_worker_roles": list(required_worker_roles(run["request_text"])),
                    **({"selected_custom_advice": custom_context} if custom_context else {}),
                    "rule": "依 required_worker_roles 顺序委派；每步只依赖前一步。",
                },
                ExecutionPlan, started,
                lambda value: self._check_plan(value, run["request_text"]),
            )
            ordered = validate_execution_plan(plan)
            if len(ordered) + 1 > self.config.max_agent_steps:
                raise ValueError("agent_step_limit")
            for step in ordered:
                self.repository.create_run_step(
                    run_id, step.step_id, step.agent, step.depends_on
                )
                self.repository.append_trace(
                    "delegated",
                    {"agent": step.agent, "depends_on": step.depends_on},
                    run_id=run_id, step_id=step.step_id,
                )
            for step in ordered:
                current_step = step.step_id
                self._check_deadline(started)
                payload = self._payload(run, step, completed, ordered)
                if step.agent == "learning" and youtube_resources is not None:
                    payload["youtube_resources"] = youtube_resources
                if custom_context and step.agent in {"learning", "life", "schedule"}:
                    payload["selected_custom_advice"] = custom_context
                result = self._run_step(
                    run_id, step.step_id, step.agent, payload,
                    WORKER_SCHEMAS[step.agent], started,
                    lambda value, role=step.agent: self._validate_result(role, value, payload, ordered),
                )
                completed[step.agent] = result
            draft = self._draft(run_id, completed, youtube_resources)
            self.repository.update_run_status(
                run_id, "waiting_approval", draft=draft.model_dump(mode="json")
            )
            self.repository.append_trace(
                "draft_ready",
                {"task_count": len(draft.tasks), "conflicts": len(draft.conflicts)},
                run_id=run_id,
            )
            return draft
        except Exception as exc:
            error_code = self._error_code(exc)
            for step in self.repository.list_run_steps(run_id):
                if step["status"] == "pending":
                    self.repository.update_run_step(
                        run_id, step["step_id"], "skipped", error_code="dependency_failed"
                    )
                    self.repository.append_trace(
                        "step_skipped", {"reason": "dependency_failed"},
                        run_id=run_id, step_id=step["step_id"],
                    )
            self.repository.update_run_status(run_id, "failed")
            self.repository.append_trace(
                "run_failed", {"step_id": current_step, "error_code": error_code},
                run_id=run_id,
            )
            raise

    def commit_plan(self, run_id: str, revision: int, approval_context: Any) -> list[dict[str, Any]]:
        return self.gateway.invoke(
            "task", "commit_approved_plan", {"run_id": run_id, "revision": revision},
            run_id=run_id, approval_context=approval_context,
        ).items

    def execute_feedback(self, run_id: str) -> list[dict[str, Any]]:
        run = self.repository.get_run(run_id)
        if run["kind"] != "feedback" or run["status"] != "pending":
            raise ValueError("feedback_run_not_pending")
        feedback = self.repository.feedback_for_run(run_id)
        task = self.repository.get_task(feedback["task_id"])
        started = time.monotonic()
        self.repository.set_run_config_snapshot(run_id, {
            "model_id": self.config.deepseek_model_id,
            "timezone": self.config.timezone,
            "agent_versions": {"memory": self.registry.get_role("memory")["version"]},
        })
        self.repository.update_run_status(run_id, "running")
        self.repository.append_trace("run_started", {"kind": "feedback"}, run_id=run_id)
        self.repository.create_run_step(run_id, "memory_feedback", "memory", [])
        try:
            batch = self._run_step(
                run_id, "memory_feedback", "memory",
                {
                    "feedback_id": feedback["id"],
                    "feedback_text": feedback["body"],
                    "completed_task": {"id": task["id"], "title": task["title"]},
                    "timezone": self.config.timezone,
                    "rule": "只提出候选记忆，不自行批准。",
                },
                MemoryProposalBatch, started,
                lambda value: [
                    normalize_proposal(item, feedback["id"], feedback["body"])
                    for item in value.proposals
                ],
            )
            normalized = [
                normalize_proposal(item, feedback["id"], feedback["body"])
                for item in batch.proposals
            ]
            proposals = self.repository.save_memory_proposals(run_id, feedback["id"], normalized)
            self.repository.append_trace(
                "memory_proposals_ready", {"proposal_count": len(proposals)}, run_id=run_id
            )
            return proposals
        except Exception as exc:
            self.repository.update_run_status(run_id, "failed")
            self.repository.append_trace(
                "run_failed", {"step_id": "memory_feedback", "error_code": self._error_code(exc)},
                run_id=run_id,
            )
            raise

    def _run_step(
        self, run_id: str, step_id: str, role: AgentRole,
        payload: dict[str, Any], schema: type[BaseModel],
        run_started: float, checker: Callable[[BaseModel], Any],
    ) -> BaseModel:
        self._check_deadline(run_started)
        self.repository.update_run_step(run_id, step_id, "running")
        self.repository.append_trace(
            "step_started", {"agent": role}, run_id=run_id,
            step_id=step_id, actor="agent",
        )
        step_started = time.monotonic()
        source_id = self.repository.get_run(run_id).get("retry_of_run_id")
        fingerprint = self._input_fingerprint(role, payload, schema)
        checkpoint = self.repository.find_checkpoint(
            source_id, step_id, role, schema.__name__, fingerprint
        ) if source_id else None
        checked = None
        if checkpoint:
            try:
                candidate = schema.model_validate_json(checkpoint["output_json"])
                checker(candidate)
                if self._input_fingerprint(role, payload, schema) == fingerprint:
                    checked = candidate
                    self.repository.append_trace(
                        "checkpoint_reused", {"source_run_id": checkpoint["run_id"], "agent": role},
                        run_id=run_id, step_id=step_id, actor="system",
                    )
            except Exception:
                self.repository.append_trace(
                    "checkpoint_rejected", {"agent": role}, run_id=run_id, step_id=step_id,
                )
        if checked is None:
            for attempt in range(1, 4):
                attempt_started = time.monotonic()
                error = None
                try:
                    self._check_deadline(run_started)
                    bound = self.gateway.bind(
                        role, run_id, step_id, self.config.max_tool_calls_per_agent
                    )
                    output = self.runner.run(role, payload, schema, bound)
                    if bound.failure_code == "permission_denied":
                        raise PermissionError("tool_denied_during_step")
                    if bound.failure_code == "tool_error":
                        raise RuntimeError("tool_failed_during_step")
                    if time.monotonic() - attempt_started > self.config.model_timeout_seconds:
                        raise TimeoutError("model_timeout")
                    self._check_deadline(run_started)
                    if not isinstance(output, schema):
                        raise ValueError("invalid_model_output")
                    checked = schema.model_validate(output.model_dump())
                    checker(checked)
                except Exception as exc:
                    error = exc
                consume = getattr(self.runner, "consume_usage", None)
                requests = consume() if callable(consume) else []
                if requests:
                    self.repository.record_model_usage(
                        run_id, step_id, attempt, requests,
                        self._error_code(error) if error else None,
                    )
                if error is None:
                    break
                code = self._error_code(error)
                if attempt < 3 and self._is_transient(error):
                    self.repository.append_trace(
                        "step_attempt_retry", {"agent": role, "attempt": attempt,
                                               "error_code": code},
                        run_id=run_id, step_id=step_id, actor="agent",
                        duration_ms=int((time.monotonic() - attempt_started) * 1000),
                    )
                    time.sleep(0.1 * attempt)
                    continue
                duration = int((time.monotonic() - step_started) * 1000)
                self.repository.update_run_step(run_id, step_id, "failed", error_code=code)
                self.repository.append_trace(
                    "step_failed", {"agent": role, "error_code": code, "attempts": attempt},
                    run_id=run_id, step_id=step_id, actor="agent", duration_ms=duration,
                )
                raise error
        duration = int((time.monotonic() - step_started) * 1000)
        result = AgentResult(
            step_id=step_id, payload=checked,
            reason=getattr(checked, "explanation", getattr(checked, "intent", "completed")),
            referenced_ids=[item.memory_id for item in checked.memories]
            if isinstance(checked, MemoryContext) else [],
        )
        if self._input_fingerprint(role, payload, schema) == fingerprint:
            self.repository.save_validated_checkpoint(
                run_id, step_id, role, schema.__name__, fingerprint,
                checked.model_dump(mode="json"), result.model_dump(mode="json"),
            )
        else:
            self.repository.update_run_step(
                run_id, step_id, "success", result=result.model_dump(mode="json")
            )
            self.repository.append_trace(
                "checkpoint_not_saved", {"agent": role, "reason": "read_state_changed"},
                run_id=run_id, step_id=step_id,
            )
        self.repository.append_trace(
            "step_succeeded", {"agent": role}, run_id=run_id,
            step_id=step_id, actor="agent", duration_ms=duration,
        )
        return checked

    def _input_fingerprint(
        self, role: AgentRole, payload: dict[str, Any], schema: type[BaseModel],
    ) -> str:
        role_config = self.registry.get_role(role)
        value = {
            "role": role, "schema": schema.__name__, "payload": payload,
            "role_config": {key: role_config[key] for key in ("instructions", "tool_subset", "version")},
            "model_id": self.config.deepseek_model_id,
            "read_state": self.repository.read_state_fingerprint(),
        }
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, APITimeoutError)):
            return True
        if isinstance(exc, ModelProviderError):
            if getattr(exc, "status_code", None) == 429:
                return True
            return isinstance(exc.__cause__, (TimeoutError, APITimeoutError))
        return False

    def _payload(
        self, run: dict[str, Any], step: StepSpec,
        completed: dict[str, BaseModel], ordered: list[StepSpec],
    ) -> dict[str, Any]:
        common = {
            "request": run["request_text"],
            "timezone": self.config.timezone,
            "current_local_time": self._local_now(),
            "step_id": step.step_id,
        }
        if step.agent == "memory":
            return {
                **common,
                "approved_memory_candidates": search_approved(
                    self.repository.list_approved_memories(), run["request_text"]
                ),
            }
        if step.agent == "learning":
            return {
                **common,
                "goals": self.repository.list_goals(),
                "task_progress": self.repository.list_tasks(),
                "selected_memories": self._selected_memories(completed),
                "mixed_domains": any(item.agent == "life" for item in ordered),
                "rule": "将目标拆成最多八个具体学习任务；混合请求的 item_id 以 study: 开头。",
            }
        if step.agent == "life":
            return {
                **common,
                "goals": self.repository.list_goals(),
                "existing_life_tasks": [
                    item for item in self.repository.list_tasks() if item["domain"] == "life"
                ],
                "selected_memories": self._selected_memories(completed, include_study_policy=False),
                "settings": self.repository.list_settings(),
                "rule": "仅拆生活目标；每个 item_id 以 life: 开头，说明用户请求中的来源。",
            }
        if step.agent == "schedule":
            avoid_ranges, avoid_ids = approved_study_avoid_ranges(
                self.repository.list_approved_memories()
            )
            if "learning" not in completed:
                avoid_ranges, avoid_ids = [], []
            time_blocks = self.repository.list_time_blocks()
            local_zone = ZoneInfo(self.config.timezone)
            return {
                **common,
                "learning_plan": self._json(completed.get("learning")),
                "life_plan": self._json(completed.get("life")),
                "allowed_item_ids": [
                    item.item_id for role in ("learning", "life") if role in completed
                    for item in completed[role].items
                ],
                "time_blocks": time_blocks,
                "available_local_windows": [
                    {
                        "start_at": datetime.fromisoformat(block["start_at_utc"])
                        .astimezone(local_zone).isoformat(),
                        "end_at": datetime.fromisoformat(block["end_at_utc"])
                        .astimezone(local_zone).isoformat(),
                    }
                    for block in time_blocks if block["kind"] == "available"
                ],
                "scheduled_tasks": [
                    item for item in self.repository.list_tasks()
                    if item["start_at_utc"] and item["status"] != "completed"
                ],
                "settings": self.repository.list_settings(),
                "selected_memories": self._selected_memories(
                    completed, include_study_policy="learning" in completed
                ),
                "hard_time_rules": [
                    {"memory_id": memory_id, "start": start.strftime("%H:%M"),
                     "end": end.strftime("%H:%M")}
                    for (start, end), memory_id in zip(avoid_ranges, avoid_ids)
                ],
                "rule": "仅在明确可用的时段安排；学习任务遵守 hard_time_rules；无法确定时间就不生成 assignment。",
            }
        return {
            **common,
            "learning_plan": self._json(completed.get("learning")),
            "life_plan": self._json(completed.get("life")),
            "schedule_proposal": self._json(completed.get("schedule")),
            "expected_task_sources": [
                {"item_id": item.item_id, "domain": domain,
                 "source_step_id": next(step.step_id for step in ordered if step.agent == role),
                 "estimated_minutes": item.estimated_minutes, "priority": item.priority}
                for role, domain in (("learning", "study"), ("life", "life"))
                for item in (completed[role].items if role in completed else [])
            ],
            "valid_source_step_ids": [item.step_id for item in ordered],
            "rule": "逐项复制 expected_task_sources 的 item_id、domain、source_step_id、estimated_minutes、priority；只生成草案，不提交。",
        }

    def _validate_result(
        self, role: str, value: BaseModel, payload: dict[str, Any],
        ordered: list[StepSpec],
    ) -> None:
        if role == "memory":
            allowed = {item["id"] for item in payload["approved_memory_candidates"]}
            if any(item.memory_id not in allowed for item in value.memories):
                raise ValueError("unapproved_memory_reference")
        elif role == "learning":
            ids = [item.item_id for item in value.items]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate_learning_item")
            if payload["mixed_domains"] and any(not item.startswith("study:") for item in ids):
                raise ValueError("invalid_study_item_id")
            resources = payload.get("youtube_resources")
            chosen = [item.resource_video_id for item in value.items if item.resource_video_id]
            if resources is not None:
                allowed_videos = {item["video_id"] for item in resources}
                if not chosen or any(video_id not in allowed_videos for video_id in chosen):
                    raise ValueError("invalid_youtube_resource_reference")
            elif chosen:
                raise ValueError("youtube_resource_not_selected")
        elif role == "life":
            ids = [item.item_id for item in value.items]
            if len(ids) != len(set(ids)) or any(not item.startswith("life:") for item in ids):
                raise ValueError("invalid_life_item_id")
        elif role == "schedule":
            learning = payload.get("learning_plan")
            life = payload.get("life_plan")
            allowed = ({item["item_id"] for item in learning["items"]} if learning else set())
            if life:
                allowed.update(item["item_id"] for item in life["items"])
            ids = [item.item_id for item in value.assignments]
            if len(ids) != len(set(ids)) or any(item not in allowed for item in ids):
                raise ValueError("unknown_schedule_item")
        elif role == "task":
            learning = payload.get("learning_plan")
            life = payload.get("life_plan")
            source_steps = {step.agent: step.step_id for step in ordered}
            allowed = {
                item["item_id"]: ("study", source_steps["learning"], item)
                for item in learning["items"]
            } if learning else {}
            allowed.update({
                item["item_id"]: ("life", source_steps["life"], item)
                for item in life["items"]
            } if life else {})
            ids = [item.draft_item_id for item in value.tasks]
            if len(ids) != len(set(ids)) or set(ids) != set(allowed):
                raise ValueError("task_items_do_not_match_sources")
            for task in value.tasks:
                if any((task.resource_url, task.resource_source, task.resource_channel,
                        task.resource_duration_seconds)):
                    raise ValueError("task_agent_cannot_assign_external_resource")
                domain, step_id, source = allowed[task.draft_item_id]
                if task.domain != domain or task.source_step_id != step_id:
                    raise ValueError(
                        f"invalid_task_source_step:{task.draft_item_id}:"
                        f"{task.domain}/{task.source_step_id}:expected:{domain}/{step_id}"
                    )
                if (task.estimated_minutes != source["estimated_minutes"]
                    or task.priority != source["priority"]):
                    raise ValueError("task_source_details_changed")

    def _draft(self, run_id: str, completed: dict[str, BaseModel],
               youtube_resources: list[dict[str, Any]] | None = None) -> PlanDraft:
        task_plan = completed.get("task")
        schedule = completed.get("schedule")
        memory = completed.get("memory")
        assignments = {item.item_id: item for item in schedule.assignments} if schedule else {}
        blocks = self.repository.list_time_blocks()
        avoid_ranges, avoid_ids = approved_study_avoid_ranges(
            self.repository.list_approved_memories()
        )
        if "learning" not in completed:
            avoid_ranges, avoid_ids = [], []
        available = [self._interval(item) for item in blocks if item["kind"] == "available"]
        busy = [self._interval(item) for item in blocks if item["kind"] == "busy"]
        existing = [
            self._interval(item) for item in self.repository.list_tasks()
            if item["start_at_utc"] and item["status"] != "completed"
        ]
        conflicts = list(schedule.conflicts) if schedule else []
        tasks: list[TaskDraft] = []
        resources_by_id = {item["video_id"]: item for item in youtube_resources or []}
        learning_items = {item.item_id: item for item in completed["learning"].items} if "learning" in completed else {}
        for task in task_plan.tasks if task_plan else []:
            assignment = assignments.get(task.draft_item_id)
            start_at = assignment.start_at if assignment else None
            end_at = assignment.end_at if assignment else None
            if assignment:
                candidate = Interval(start_at, end_at)
                issues = validate_schedule(
                    candidate, availability=available, busy=busy,
                    tasks=existing, due_at=task.due_at,
                    avoid_local_ranges=avoid_ranges if task.domain == "study" else [],
                    timezone_name=self.config.timezone,
                )
                if (end_at - start_at).total_seconds() < task.estimated_minutes * 60:
                    issues.append("insufficient_duration")
                if issues:
                    conflicts.append(f"{task.draft_item_id}: {', '.join(issues)}")
                    start_at = end_at = None
                else:
                    existing.append(candidate)
            learning_item = learning_items.get(task.draft_item_id)
            video = resources_by_id.get(learning_item.resource_video_id) if learning_item else None
            tasks.append(task.model_copy(update={
                "start_at": start_at, "end_at": end_at,
                "resource_url": video["url"] if video else None,
                "resource_source": video["source"] if video else None,
                "resource_channel": video["channel"] if video else None,
                "resource_duration_seconds": video["duration_seconds"] if video else None,
            }))
        if not assignments and tasks:
            conflicts.append("没有可用的已验证时间安排；任务保留为未排程草案")
        return PlanDraft(
            run_id=run_id, revision=0, tasks=tasks, conflicts=conflicts,
            memory_ids=sorted(set(
                ([item.memory_id for item in memory.memories] if memory else []) + avoid_ids
            )),
            explanations=[
                f"{role}: {completed[role].explanation}"
                for role in ("memory", "learning", "life", "schedule", "task") if role in completed
            ] + [
                f"{role} 来源 {item.item_id}: {item.reason}"
                for role in ("learning", "life") if role in completed
                for item in completed[role].items
            ] + [
                f"YouTube 资料 {item.item_id}: {resources_by_id[item.resource_video_id]['title']} · "
                f"{resources_by_id[item.resource_video_id]['channel']} · "
                f"{resources_by_id[item.resource_video_id]['duration_seconds']} 秒 · "
                f"{resources_by_id[item.resource_video_id]['url']} · 来源 YouTube Data API v3；仅使用元数据，未读取视频内容"
                for item in learning_items.values() if item.resource_video_id in resources_by_id
            ] + [f"已应用记忆 {memory_id}：避开学习时段"
                 for memory_id in avoid_ids],
        )

    def _selected_memories(
        self, completed: dict[str, BaseModel], *, include_study_policy: bool = True
    ) -> list[dict[str, Any]]:
        context = completed.get("memory")
        ids = {item.memory_id for item in context.memories} if context else set()
        memories = self.repository.list_approved_memories()
        if include_study_policy:
            _, policy_ids = approved_study_avoid_ranges(memories)
            ids.update(policy_ids)
        return [item for item in memories if item["id"] in ids
                and (include_study_policy or item["kind"] != "study_time_avoid")]

    @staticmethod
    def _interval(row: dict[str, Any]) -> Interval:
        return Interval(
            datetime.fromisoformat(row["start_at_utc"]),
            datetime.fromisoformat(row["end_at_utc"]),
        )

    @staticmethod
    def _json(value: BaseModel | None) -> dict[str, Any] | None:
        return value.model_dump(mode="json") if value is not None else None

    def _local_now(self) -> str:
        return datetime.now(ZoneInfo(self.config.timezone)).replace(second=0, microsecond=0).isoformat()

    def _check_deadline(self, started: float) -> None:
        if time.monotonic() - started > self.config.run_timeout_seconds:
            raise TimeoutError("run_timeout")

    @staticmethod
    def _check_plan(plan: ExecutionPlan, request_text: str) -> None:
        ordered = validate_execution_plan(plan)
        required = required_worker_roles(request_text)
        if tuple(step.agent for step in ordered) != required:
            raise ValueError("missing_required_worker_role")

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, ModelProviderError) and getattr(exc, "status_code", None) == 429:
            return "rate_limited"
        if isinstance(exc, ModelProviderError) and isinstance(exc.__cause__, (TimeoutError, APITimeoutError)):
            return "timeout"
        if isinstance(exc, PermissionError):
            return "permission_denied"
        if isinstance(exc, ValueError):
            return "invalid_output"
        return "agent_error"
