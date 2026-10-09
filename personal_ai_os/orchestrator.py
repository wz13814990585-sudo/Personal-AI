"""Validate model-proposed delegation before any worker Agent runs."""

from __future__ import annotations

from .contracts import ExecutionPlan, StepSpec


ROLE_ORDER = ("memory", "learning", "life", "schedule", "task")


def required_worker_roles(request_text: str) -> tuple[str, ...]:
    """Make explicit study/life intent testable before accepting a model graph."""
    text = request_text.casefold()
    life = any(word in text for word in (
        "生活", "运动", "锻炼", "健身", "跑步", "散步", "家务", "买菜", "购物",
        "做饭", "洗衣", "打扫", "清洁", "exercise", "workout", "grocery",
        "groceries", "chores",
    ))
    study = any(word in text for word in (
        "学习", "面试", "考试", "复习", "课程", "备考", "study", "learn", "interview",
    ))
    domain_roles = ("learning", "life") if study and life else ("life",) if life else ("learning",)
    return ("memory", *domain_roles, "schedule", "task")


def validate_execution_plan(plan: ExecutionPlan) -> list[StepSpec]:
    steps = plan.steps
    if not steps:
        raise ValueError("empty_execution_plan")
    ids = [step.step_id for step in steps]
    roles = [step.agent for step in steps]
    if len(ids) != len(set(ids)) or len(roles) != len(set(roles)):
        raise ValueError("duplicate_execution_step")
    if any(role not in ROLE_ORDER for role in roles):
        raise ValueError("invalid_worker_role")
    by_id = {step.step_id: step for step in steps}
    by_role = {step.agent: step for step in steps}
    for step in steps:
        if step.step_id in step.depends_on or len(step.depends_on) != len(set(step.depends_on)):
            raise ValueError("invalid_step_dependency")
        if any(dependency not in by_id for dependency in step.depends_on):
            raise ValueError("unknown_step_dependency")

    ordered: list[StepSpec] = []
    remaining = dict(by_id)
    while remaining:
        ready = [
            step for step in remaining.values()
            if all(dependency not in remaining for dependency in step.depends_on)
        ]
        if not ready:
            raise ValueError("cyclic_execution_plan")
        ready.sort(key=lambda step: ROLE_ORDER.index(step.agent))
        for step in ready:
            ordered.append(step)
            del remaining[step.step_id]

    def ancestors(step_id: str) -> set[str]:
        direct = by_id[step_id].depends_on
        return set(direct).union(*(ancestors(item) for item in direct))

    selected = [role for role in ROLE_ORDER if role in by_role]
    for previous, current in zip(selected, selected[1:]):
        if by_role[previous].step_id not in ancestors(by_role[current].step_id):
            raise ValueError("missing_required_dependency")
    return ordered
