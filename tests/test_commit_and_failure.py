import pytest

from personal_ai_os.contracts import PlanDraft, TaskCreate, TaskDraft
from personal_ai_os.tool_gateway import ApprovalContext

from conftest import moment


REQUEST = "明晚七点有 AI Agent 面试，请制定学习计划并安排任务"


def test_edit_revision_approval_and_duplicate_click_are_safe(flow_system):
    repository, _, gateway, _, _, service = flow_system
    draft = service.start_plan(REQUEST)
    assert repository.list_tasks() == []
    changed = draft.tasks[0].model_copy(update={"title": "Review interview questions"})
    edited = service.edit_plan(draft.run_id, draft.revision, [changed])
    assert edited.revision == 1
    with pytest.raises(ValueError, match="stale_plan_revision"):
        service.edit_plan(draft.run_id, 0, [changed])
    with pytest.raises(ValueError, match="stale_plan_revision"):
        service.approve_plan(draft.run_id, 0)
    assert repository.list_tasks() == []

    with pytest.raises(PermissionError, match="approval_required"):
        gateway.invoke(
            "task", "commit_approved_plan", {"run_id": draft.run_id, "revision": 1},
            run_id=draft.run_id,
            approval_context=ApprovalContext(draft.run_id, 1, object()),
        )
    assert repository.list_tasks() == []
    first = service.approve_plan(draft.run_id, 1)
    second = service.approve_plan(draft.run_id, 1)
    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert len(repository.list_tasks()) == 1
    assert first[0]["title"] == "Review interview questions"
    assert repository.get_run(draft.run_id)["status"] == "success"
    with pytest.raises(ValueError, match="plan_not_waiting_approval"):
        service.reject_plan(draft.run_id, 1)


def test_commit_rechecks_conflict_and_allows_edit_to_safe_slot(flow_system):
    repository, _, _, _, _, service = flow_system
    draft = service.start_plan(REQUEST)
    repository.create_task(TaskCreate(
        title="Existing appointment", start_at=moment(10, 22), end_at=moment(10, 23)
    ))
    with pytest.raises(ValueError, match="task_overlap"):
        service.approve_plan(draft.run_id, 0)
    assert len(repository.list_tasks()) == 1
    assert repository.get_run(draft.run_id)["status"] == "waiting_approval"

    revised_task = draft.tasks[0].model_copy(update={
        "start_at": moment(11, 4), "end_at": moment(11, 5),
    })
    edited = service.edit_plan(draft.run_id, 0, [revised_task])
    assert edited.conflicts == []
    committed = service.approve_plan(draft.run_id, edited.revision)
    assert len(committed) == 1
    assert len(repository.list_tasks()) == 2
    assert committed[0]["start_at_utc"] == moment(11, 4).isoformat()


def test_plan_edit_revalidates_fields_and_reports_current_conflicts(flow_system):
    repository, _, _, _, _, service = flow_system
    draft = service.start_plan(REQUEST)
    invalid = draft.tasks[0].model_copy(update={
        "start_at": moment(10, 23), "end_at": moment(10, 22),
    })
    with pytest.raises(ValueError, match="start_at must be earlier"):
        service.edit_plan(draft.run_id, 0, [invalid])
    assert repository.get_run(draft.run_id)["revision"] == 0

    outside = draft.tasks[0].model_copy(update={
        "start_at": moment(11, 6), "end_at": moment(11, 7),
    })
    edited = service.edit_plan(draft.run_id, 0, [outside])
    assert any("outside_availability" in issue for issue in edited.conflicts)
    with pytest.raises(ValueError, match="outside_availability"):
        service.approve_plan(draft.run_id, edited.revision)
    assert repository.list_tasks() == []


def test_multi_item_commit_rolls_back_if_later_item_conflicts(flow_system):
    repository, _, _, _, _, service = flow_system
    run = repository.create_run(REQUEST)
    repository.update_run_status(run["id"], "running")
    repository.create_run_step(run["id"], "learning", "learning", [])
    repository.update_run_step(run["id"], "learning", "success", result={})
    first = TaskDraft(
        draft_item_id="a", title="First", estimated_minutes=60, priority="high",
        start_at=moment(10, 22), end_at=moment(10, 23), source_step_id="learning",
    )
    second = first.model_copy(update={"draft_item_id": "b", "title": "Second"})
    draft = PlanDraft(run_id=run["id"], revision=0, tasks=[first, second])
    repository.update_run_status(run["id"], "waiting_approval", draft=draft.model_dump(mode="json"))
    with pytest.raises(ValueError, match="task_overlap"):
        service.approve_plan(run["id"], 0)
    assert repository.list_tasks() == []
    assert repository.get_run(run["id"])["status"] == "waiting_approval"


def test_rejection_and_feedback_guard(flow_system):
    repository, _, _, _, _, service = flow_system
    draft = service.start_plan(REQUEST)
    service.reject_plan(draft.run_id, 0)
    assert repository.get_run(draft.run_id)["status"] == "cancelled"
    with pytest.raises(ValueError, match="plan_not_waiting_approval"):
        service.approve_plan(draft.run_id, 0)
    assert repository.list_tasks() == []

    task = repository.create_task(TaskCreate(title="Not done"))
    with pytest.raises(ValueError, match="feedback_requires_completed_task"):
        service.submit_feedback(task["id"], "不要在早上安排学习")
    assert repository.list_feedback() == []
