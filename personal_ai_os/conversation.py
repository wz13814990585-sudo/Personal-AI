"""Bounded conversation summaries for the UI and follow-up planning."""

from __future__ import annotations

from typing import Any

from .contracts import PlanDraft


def recent_plan_history(
    runs: list[dict[str, Any]], conversation_id: str | None,
    *, exclude_run_id: str | None = None,
) -> list[dict[str, str]]:
    if not conversation_id:
        return []
    history: list[dict[str, str]] = []
    for run in runs:
        if run["conversation_id"] != conversation_id or run["kind"] != "planning":
            continue
        if run["id"] == exclude_run_id:
            continue
        if run["draft_json"]:
            draft = PlanDraft.model_validate_json(run["draft_json"])
            titles = "、".join(task.title for task in draft.tasks[:3])
            summary = f"建议任务：{titles or '无'}；冲突 {len(draft.conflicts)} 项"
            if draft.memory_ids:
                summary += "；依据记忆 " + "、".join(draft.memory_ids[:3])
        else:
            summary = "本次运行未产生计划草案"
        history.append({
            "request": run["request_text"][:300],
            "status": run["status"],
            "summary": summary[:400],
        })
        if len(history) == 3:
            break
    return history
