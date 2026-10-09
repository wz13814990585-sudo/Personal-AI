"""Code-level read-only permission ceiling for user-defined Agents."""

from __future__ import annotations

from .contracts import CustomAgentCreate


CUSTOM_AGENT_READ_TOOLS = frozenset({
    "read_goals", "read_task_progress", "read_time_blocks",
    "read_scheduled_tasks", "read_settings", "search_approved_memories",
})
BUILTIN_AGENT_NAMES = frozenset({
    "orchestrator", "memory", "learning", "life", "schedule", "task",
})


def validate_custom_agent(data: CustomAgentCreate) -> CustomAgentCreate:
    if data.name.casefold() in BUILTIN_AGENT_NAMES:
        raise ValueError("custom_agent_name_reserved")
    if not data.tool_subset <= CUSTOM_AGENT_READ_TOOLS:
        raise PermissionError("custom_agent_tool_exceeds_read_only_maximum")
    return data
