"""Reviewable weekday suggestions derived from a validated Life task draft."""

from __future__ import annotations

import re
from uuid import NAMESPACE_URL, uuid5

from .contracts import PlanDraft


THREE_PER_WEEK = re.compile(r"(?:每周|每星期|一周).{0,8}(?:三|3)次")
EXERCISE = re.compile(r"运动|锻炼|健身|跑步|慢跑|游泳|训练")


def suggestion_rule_id(run_id: str, item_id: str) -> str:
    return uuid5(NAMESPACE_URL, f"personal-ai-os:recurrence:{run_id}:{item_id}").hex


def weekly_three_suggestions(request_text: str, draft: PlanDraft,
                             timezone_name: str) -> list[dict]:
    if not THREE_PER_WEEK.search(request_text):
        return []
    life_tasks = [item for item in draft.tasks
                  if item.domain == "life" and item.source_step_id == "life"]
    if not life_tasks or not EXERCISE.search(request_text):
        return []
    task = next((item for item in life_tasks if EXERCISE.search(item.title)
                 or any(line.startswith(f"life 来源 {item.draft_item_id}:")
                        and EXERCISE.search(line) for line in draft.explanations)), None)
    if task is None:
        return []
    return [{
        "item_id": task.draft_item_id,
        "rule_id": suggestion_rule_id(draft.run_id, task.draft_item_id),
        "title": task.title,
        "weekdays": [0, 2, 4],
        "timezone": timezone_name,
        "estimated_minutes": task.estimated_minutes,
        "priority": task.priority,
        "reason": "每周三次：建议周一、周三、周五，间隔安排；确认前不会创建规则。",
    }]
