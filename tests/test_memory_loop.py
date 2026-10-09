import json

import pytest

from personal_ai_os.agent_registry import AgentRegistry
from personal_ai_os.contracts import (
    MemoryProposal, MemoryProposalBatch, ScheduleAssignment, ScheduleProposal, TaskCreate,
)
from personal_ai_os.harness import Harness
from personal_ai_os.memory import search_approved
from personal_ai_os.services import PersonalAIService
from personal_ai_os.storage import Repository
from personal_ai_os.tool_gateway import ToolGateway

from conftest import moment


REQUEST = "明晚七点有 AI Agent 面试，请制定学习计划并安排任务"
FEEDBACK = "今天准备面试时我发现：不要在早上安排学习，下午更合适。"


def test_feedback_review_changes_next_plan_and_survives_reopen(flow_system):
    repository, _, _, runner, harness, service = flow_system
    first = service.start_plan(REQUEST)
    assert first.tasks[0].start_at == moment(10, 22)
    committed = service.approve_plan(first.run_id, first.revision)
    service.complete_task(committed[0]["id"])

    feedback, proposals = service.submit_feedback(committed[0]["id"], FEEDBACK)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert json.loads(proposal["value_json"]) == {"start": "00:00", "end": "12:00"}
    assert repository.get_run(feedback["run_id"])["status"] == "waiting_approval"
    assert repository.list_approved_memories() == []
    before_review = service.start_plan(REQUEST)
    assert before_review.tasks[0].start_at == moment(10, 22)
    assert before_review.memory_ids == []

    with pytest.raises(ValueError, match="invalid_study_time_range"):
        service.edit_memory_proposal(proposal["id"], {"start": "18:00", "end": "09:00"})
    edited = service.edit_memory_proposal(
        proposal["id"], {"start": "00:00", "end": "12:00"}
    )
    assert json.loads(edited["value_json"])["end"] == "12:00"
    memory = service.resolve_memory(proposal["id"], approve=True)
    assert memory["source_feedback_id"] == feedback["id"]
    assert repository.get_run(feedback["run_id"])["status"] == "success"
    with pytest.raises(ValueError, match="proposal_already_resolved"):
        service.resolve_memory(proposal["id"], approve=True)

    with pytest.raises(ValueError, match="avoided_local_time"):
        service.approve_plan(before_review.run_id, before_review.revision)
    assert repository.get_run(before_review.run_id)["status"] == "waiting_approval"
    assert len(repository.list_tasks()) == 1

    reopened = Repository(repository.path)
    reopened.initialize()
    registry = AgentRegistry(reopened)
    gateway = ToolGateway(reopened, registry, harness.config.timezone)
    reopened_service = PersonalAIService(
        reopened, gateway, Harness(reopened, registry, gateway, runner, harness.config)
    )
    after_review = reopened_service.start_plan(REQUEST)
    assert after_review.tasks[0].start_at == moment(11, 4)
    assert memory["id"] in after_review.memory_ids
    assert any(memory["id"] in text for text in after_review.explanations)

    reopened_service.update_memory(memory["id"], {"start": "14:00", "end": "18:00"})
    after_edit = reopened_service.start_plan(REQUEST)
    assert after_edit.tasks[0].start_at == moment(10, 22)
    reopened_service.delete_memory(memory["id"])
    assert reopened.list_approved_memories() == []
    after_delete = reopened_service.start_plan(REQUEST)
    assert after_delete.tasks[0].start_at == moment(10, 22)
    assert after_delete.memory_ids == []


def test_rejected_candidate_never_enters_memory(flow_system):
    repository, _, _, _, _, service = flow_system
    task = repository.create_task(TaskCreate(title="Completed study"))
    service.complete_task(task["id"])
    feedback, proposals = service.submit_feedback(task["id"], FEEDBACK)
    assert service.resolve_memory(proposals[0]["id"], approve=False) is None
    assert repository.get_run(feedback["run_id"])["status"] == "success"
    assert repository.list_approved_memories() == []
    assert service.start_plan(REQUEST).memory_ids == []


def test_feedback_failure_keeps_original_text_and_marks_run_failed(flow_system):
    repository, _, _, runner, _, service = flow_system
    task = repository.create_task(TaskCreate(title="Completed study"))
    service.complete_task(task["id"])
    runner.fail_feedback = True
    with pytest.raises(TimeoutError, match="injected feedback timeout"):
        service.submit_feedback(task["id"], FEEDBACK)
    saved = repository.list_feedback()
    assert len(saved) == 1 and saved[0]["body"] == FEEDBACK
    assert repository.get_run(saved[0]["run_id"])["status"] == "failed"
    assert repository.list_memory_proposals() == []
    assert repository.list_approved_memories() == []


def test_approved_rule_blocks_morning_even_if_schedule_agent_suggests_it(flow_system):
    repository, _, _, runner, _, service = flow_system
    task = repository.create_task(TaskCreate(title="Completed study"))
    service.complete_task(task["id"])
    _, proposals = service.submit_feedback(task["id"], FEEDBACK)
    memory = service.resolve_memory(proposals[0]["id"], approve=True)

    original_run = runner.run
    def stubborn_run(role, payload, output_schema, tools):
        if role == "schedule":
            return ScheduleProposal(assignments=[ScheduleAssignment(
                item_id="review", start_at=moment(10, 22), end_at=moment(10, 23),
                reason="Ignoring preference",
            )], explanation="Morning suggestion")
        return original_run(role, payload, output_schema, tools)
    runner.run = stubborn_run

    draft = service.start_plan(REQUEST)
    assert draft.tasks[0].start_at is None
    assert any("avoided_local_time" in issue for issue in draft.conflicts)
    assert memory["id"] in draft.memory_ids


def test_memory_search_prioritizes_approved_rules_and_related_text():
    rows = [
        {"id": "unrelated", "status": "approved", "kind": "text_preference",
         "value_json": '{"text":"喜欢做饭"}', "updated_at_utc": "2026-10-08T03:00:00+00:00"},
        {"id": "related", "status": "approved", "kind": "text_preference",
         "value_json": '{"text":"下午学习效率高"}', "updated_at_utc": "2026-10-08T02:00:00+00:00"},
        {"id": "rule", "status": "approved", "kind": "study_time_avoid",
         "value_json": '{"start":"00:00","end":"12:00"}',
         "updated_at_utc": "2026-10-08T01:00:00+00:00"},
        {"id": "pending", "status": "pending", "kind": "text_preference",
         "value_json": '{"text":"学习"}', "updated_at_utc": "2026-10-08T04:00:00+00:00"},
    ]
    assert [row["id"] for row in search_approved(rows, "安排学习", 2)] == ["rule", "related"]


def test_multiple_candidates_finish_only_after_every_decision(flow_system):
    repository, _, _, runner, _, service = flow_system
    task = repository.create_task(TaskCreate(title="Completed study"))
    service.complete_task(task["id"])
    original_run = runner.run
    def two_proposals(role, payload, output_schema, tools):
        if output_schema is MemoryProposalBatch:
            return MemoryProposalBatch(proposals=[
                MemoryProposal(
                    feedback_id=payload["feedback_id"], kind="study_time_avoid",
                    value={"start": "00:00", "end": "12:00"},
                    source_excerpt="不要在早上安排学习", explanation="Avoid morning",
                ),
                MemoryProposal(
                    feedback_id=payload["feedback_id"], kind="text_preference",
                    value={"text": "下午更合适"},
                    source_excerpt="下午更合适", explanation="Afternoon preferred",
                ),
            ])
        return original_run(role, payload, output_schema, tools)
    runner.run = two_proposals
    feedback, proposals = service.submit_feedback(task["id"], FEEDBACK)
    assert len(proposals) == 2
    service.resolve_memory(proposals[0]["id"], approve=True)
    assert repository.get_run(feedback["run_id"])["status"] == "waiting_approval"
    service.resolve_memory(proposals[1]["id"], approve=False)
    assert repository.get_run(feedback["run_id"])["status"] == "success"
    assert len(repository.list_approved_memories()) == 1
