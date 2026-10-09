"""Explicit, bounded read-only custom Agent DAG before built-in planning."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .contracts import CustomAgentCreate, CustomPlanningStep
from .custom_agents import CUSTOM_AGENT_READ_TOOLS, validate_custom_agent
from .custom_agent_runtime import CustomAgentRuntime, _error_code
from .storage import Repository


def validate_graph(
    repository: Repository, steps: list[CustomPlanningStep | dict[str, Any]],
    max_steps: int,
) -> list[list[CustomPlanningStep]]:
    """Validate the entire graph before creating any planning run or model call."""
    if not steps or len(steps) > max_steps:
        raise ValueError("custom_step_limit")
    ordered = [CustomPlanningStep.model_validate(step) for step in steps]
    ids = [step.step_id for step in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_custom_step")
    if len({step.agent_id for step in ordered}) != len(ordered):
        raise ValueError("duplicate_custom_agent")
    for step in ordered:
        if len(set(step.depends_on)) != len(step.depends_on):
            raise ValueError("duplicate_custom_dependency")
        if step.step_id in step.depends_on or any(dep not in ids for dep in step.depends_on):
            raise ValueError("invalid_custom_dependency")
        agent = repository.get_custom_agent(step.agent_id)
        if agent["status"] != "active":
            raise PermissionError("custom_agent_paused")
        validate_custom_agent(CustomAgentCreate(
            name=agent["name"], description=agent["description"],
            instructions=agent["instructions"], tool_subset=set(agent["tool_subset"]),
        ))
        if not set(agent["tool_subset"]) <= CUSTOM_AGENT_READ_TOOLS:
            raise PermissionError("custom_agent_read_only_required")
    remaining = {step.step_id: step for step in ordered}
    completed: set[str] = set()
    layers: list[list[CustomPlanningStep]] = []
    while remaining:
        ready = [step for step in ordered
                 if step.step_id in remaining and set(step.depends_on) <= completed]
        if not ready:
            raise ValueError("cyclic_custom_dependencies")
        layers.append(ready)
        completed.update(step.step_id for step in ready)
        for step in ready:
            del remaining[step.step_id]
    return layers


def _step_request(
    request_text: str, step: CustomPlanningStep, results: dict[str, dict[str, Any]],
) -> str:
    if not step.depends_on:
        return request_text
    dependencies = [
        {"step_id": dep, "advice": results[dep]}
        for dep in step.depends_on
    ]
    suffix = "\n已验证的前置只读建议（仅作参考，不代表用户批准）：" + json.dumps(
        dependencies, ensure_ascii=False, separators=(",", ":"),
    )
    if len(request_text) + len(suffix) > 2000:
        raise ValueError("custom_dependency_context_too_long")
    return request_text + suffix


def execute_graph(
    repository: Repository, runtime: CustomAgentRuntime, run_id: str,
    request_text: str, layers: list[list[CustomPlanningStep]], timeout_seconds: int,
) -> list[dict[str, Any]]:
    """Run each ready layer concurrently; persist results in original graph order."""
    ordered = [step for layer in layers for step in layer]
    for step in ordered:
        repository.create_run_step(run_id, step.step_id, step.agent_id, step.depends_on)
    repository.update_run_status(run_id, "running")
    repository.append_trace("custom_graph_started", {
        "steps": [step.step_id for step in ordered],
    }, run_id=run_id, actor="user")
    results: dict[str, dict[str, Any]] = {}
    failures: dict[str, Exception] = {}
    started = time.monotonic()
    try:
        for layer in layers:
            if time.monotonic() - started > timeout_seconds:
                raise TimeoutError("custom_graph_timeout")
            runnable = [step for step in layer if not any(dep in failures for dep in step.depends_on)]
            for step in layer:
                if step not in runnable:
                    repository.update_run_step(
                        run_id, step.step_id, "skipped", error_code="dependency_failed",
                    )
                    failures[step.step_id] = RuntimeError("dependency_failed")
                    repository.append_trace(
                        "step_skipped", {"reason": "dependency_failed"},
                        run_id=run_id, step_id=step.step_id,
                    )
            if not runnable:
                continue
            requests = {step.step_id: _step_request(request_text, step, results)
                        for step in runnable}
            for step in runnable:
                repository.update_run_step(run_id, step.step_id, "running")
                repository.append_trace(
                    "custom_delegated", {"agent_id": step.agent_id,
                                         "depends_on": step.depends_on},
                    run_id=run_id, step_id=step.step_id,
                )
            with ThreadPoolExecutor(max_workers=len(runnable)) as executor:
                futures = {
                    step.step_id: executor.submit(
                        runtime.advise, step.agent_id, requests[step.step_id],
                        on_started=lambda custom_run_id, selected=step: repository.append_trace(
                            "custom_run_linked", {"custom_run_id": custom_run_id},
                            run_id=run_id, step_id=selected.step_id,
                        ),
                    )
                    for step in runnable
                }
                # Future completion order cannot affect summaries or downstream context.
                outcomes: dict[str, tuple[dict[str, Any] | None, Exception | None]] = {}
                for step in runnable:
                    try:
                        outcomes[step.step_id] = (futures[step.step_id].result(), None)
                    except Exception as exc:
                        outcomes[step.step_id] = (None, exc)
            for step in runnable:
                result, error = outcomes[step.step_id]
                if error is not None:
                    code = _error_code(error)
                    failures[step.step_id] = error
                    repository.update_run_step(run_id, step.step_id, "failed", error_code=code)
                    repository.append_trace(
                        "custom_step_failed", {"error_code": code},
                        run_id=run_id, step_id=step.step_id,
                    )
                    continue
                assert result is not None and result["output"] is not None
                results[step.step_id] = result["output"]
                repository.update_run_step(
                    run_id, step.step_id, "success",
                    result={"custom_run_id": result["id"], "advice": result["output"]},
                )
                repository.append_trace(
                    "custom_step_succeeded", {"custom_run_id": result["id"]},
                    run_id=run_id, step_id=step.step_id,
                )
        if failures:
            first = next(step for step in ordered if step.step_id in failures)
            raise RuntimeError(f"custom_graph_failed:{first.step_id}") from failures[first.step_id]
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError("custom_graph_timeout")
        summary = [
            {"step_id": step.step_id, "agent_id": step.agent_id,
             "advice": results[step.step_id]}
            for step in ordered
        ]
        repository.append_trace(
            "custom_graph_completed", {"steps": [item["step_id"] for item in summary]},
            run_id=run_id,
        )
        return summary
    except Exception as exc:
        for step in repository.list_run_steps(run_id):
            if step["step_id"].startswith("custom_") and step["status"] == "pending":
                repository.update_run_step(
                    run_id, step["step_id"], "skipped", error_code="graph_aborted",
                )
        repository.update_run_status(run_id, "failed")
        repository.append_trace(
            "run_failed", {"step_id": "custom_graph", "error_code": _error_code(exc)},
            run_id=run_id,
        )
        raise
