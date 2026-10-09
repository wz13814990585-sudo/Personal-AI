"""P1 round 2: life planning, provenance, permissions, migration, and UI."""

import os
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from streamlit.testing.v1 import AppTest

from personal_ai_os import migrations
from personal_ai_os.agent_registry import AgentRegistry, ROLE_DEFINITIONS
from personal_ai_os.agents import AgnoAgentRunner
from personal_ai_os.config import load_config
from personal_ai_os.contracts import (
    ExecutionPlan, LearningItem, LearningPlan, LifeItem, LifePlan, MemoryContext,
    ScheduleAssignment, ScheduleProposal, StepSpec, TaskDraft, TaskPlan, TimeBlockCreate,
)
from personal_ai_os.harness import Harness
from personal_ai_os.orchestrator import required_worker_roles, validate_execution_plan
from personal_ai_os.services import PersonalAIService
from personal_ai_os.storage import Repository, SCHEMA
from personal_ai_os.tool_gateway import ToolGateway

from conftest import moment


MIXED = "明晚七点有 AI Agent 面试，请准备学习任务；另外每周三次运动、周末买菜，请一起规划生活任务并安排时间"
LIFE_ONLY = "请把运动和买菜拆成生活任务并安排时间"
APP = str(Path(__file__).resolve().parents[1] / "app.py")


class LifeRunner:
    def __init__(self, *, deny=False, bad_source=False, bad_life_id=False):
        self.calls = []
        self.deny = deny
        self.bad_source = bad_source
        self.bad_life_id = bad_life_id

    def run(self, role, payload, schema, tools):
        self.calls.append((role, payload))
        if role == "orchestrator":
            roles = payload["required_worker_roles"]
            return ExecutionPlan(intent="mixed" if "learning" in roles else "life", steps=[
                StepSpec(step_id=name, agent=name, purpose=name,
                         depends_on=[roles[index - 1]] if index else [])
                for index, name in enumerate(roles)
            ])
        if role == "memory":
            return MemoryContext(memories=[], explanation="No approved preferences")
        if role == "learning":
            item_id = "study:review" if payload["mixed_domains"] else "review"
            return LearningPlan(items=[LearningItem(
                item_id=item_id, title="面试复习", estimated_minutes=60,
                priority="high", reason="面试准备",
            )], explanation="Study item")
        if role == "life":
            assert all(item["status"] == "approved" for item in payload["selected_memories"])
            assert "commit_approved_plan" not in tools.available_names
            if self.deny:
                tools.invoke("commit_approved_plan", {"run_id": "fake", "revision": 0})
            items = [LifeItem(
                item_id="bad-id" if self.bad_life_id else "life:exercise",
                title="运动", estimated_minutes=60, priority="medium", reason="每周三次运动",
            )]
            if "learning" in [item for item, _ in self.calls]:
                items.append(LifeItem(
                    item_id="life:groceries", title="买菜", estimated_minutes=60,
                    priority="low", reason="周末买菜",
                ))
            return LifePlan(items=items, explanation="Life items")
        if role == "schedule":
            ids = []
            for name in ("learning_plan", "life_plan"):
                if payload[name]:
                    ids.extend(item["item_id"] for item in payload[name]["items"])
            slots = [(moment(10, 22), moment(10, 23)),
                     (moment(10, 23), moment(11, 0)),
                     (moment(11, 4), moment(11, 5))]
            return ScheduleProposal(assignments=[ScheduleAssignment(
                item_id=item_id, start_at=slots[index][0], end_at=slots[index][1],
                reason="Available window",
            ) for index, item_id in enumerate(ids)], explanation="Assigned all")
        assert role == "task"
        tasks = []
        for source_role, name, domain in (("learning", "learning_plan", "study"),
                                           ("life", "life_plan", "life")):
            if payload[name]:
                for item in payload[name]["items"]:
                    tasks.append(TaskDraft(
                        draft_item_id=item["item_id"], title=item["title"],
                        estimated_minutes=item["estimated_minutes"], priority=item["priority"],
                        domain=domain,
                        source_step_id="learning" if self.bad_source and domain == "life" else source_role,
                    ))
        return TaskPlan(tasks=tasks, explanation="Draft only")


def system(tmp_path, runner):
    repository = Repository(tmp_path / "life.sqlite3")
    repository.initialize()
    for begin, end in ((moment(10, 22), moment(11, 0)),
                       (moment(11, 4), moment(11, 5))):
        repository.create_time_block(TimeBlockCreate(kind="available", start_at=begin, end_at=end))
    registry = AgentRegistry(repository)
    config = load_config(environ={})
    gateway = ToolGateway(repository, registry, config.timezone)
    service = PersonalAIService(repository, gateway, Harness(repository, registry, gateway, runner, config))
    return repository, registry, gateway, service


def test_mixed_plan_has_six_agents_and_source_checked_tasks(tmp_path):
    runner = LifeRunner()
    repository, _, _, service = system(tmp_path, runner)
    draft = service.start_plan(MIXED)
    assert [step["agent"] for step in service.list_run_steps(draft.run_id)] == [
        "orchestrator", "memory", "learning", "life", "schedule", "task"
    ]
    assert [task.domain for task in draft.tasks] == ["study", "life", "life"]
    assert [task.source_step_id for task in draft.tasks] == ["learning", "life", "life"]
    assert [task.estimated_minutes for task in draft.tasks] == [60, 60, 60]
    assert [task.priority for task in draft.tasks] == ["high", "medium", "low"]
    assert any("每周三次运动" in line for line in draft.explanations)
    assert any("周末买菜" in line for line in draft.explanations)
    assert all(task.start_at for task in draft.tasks)
    assert repository.list_tasks() == []
    assert repository.get_run(draft.run_id)["status"] == "waiting_approval"
    saved = PersonalAIService(repository, service.gateway, service.harness).get_plan(draft.run_id)
    assert [task.domain for task in saved.tasks] == ["study", "life", "life"]
    committed = service.approve_plan(draft.run_id, draft.revision)
    assert [task["domain"] for task in committed] == ["study", "life", "life"]
    assert {task["source_run_id"] for task in committed} == {draft.run_id}
    assert service.approve_plan(draft.run_id, draft.revision) == committed


def test_life_only_and_original_study_route_are_distinct(tmp_path):
    repository, _, _, service = system(tmp_path, LifeRunner())
    life = service.start_plan(LIFE_ONLY)
    assert [step["agent"] for step in service.list_run_steps(life.run_id)] == [
        "orchestrator", "memory", "life", "schedule", "task"
    ]
    assert len(life.tasks) == 1 and life.tasks[0].domain == "life"
    assert repository.list_tasks() == []
    assert required_worker_roles("明晚面试，请学习并安排时间") == (
        "memory", "learning", "schedule", "task"
    )
    assert required_worker_roles(MIXED) == (
        "memory", "learning", "life", "schedule", "task"
    )


def test_life_cannot_write_and_bad_source_fails_without_task(tmp_path):
    repository, _, _, service = system(tmp_path, LifeRunner(deny=True))
    with pytest.raises(PermissionError, match="role_not_allowed"):
        service.start_plan(LIFE_ONLY)
    run = repository.list_runs()[0]
    assert run["status"] == "failed" and repository.list_tasks() == []
    assert any(event["event_type"] == "tool_denied" and event["step_id"] == "life"
               for event in repository.list_trace(run["id"]))

    repository2, _, _, service2 = system(tmp_path / "second", LifeRunner(bad_source=True))
    with pytest.raises(ValueError, match="invalid_task_source_step"):
        service2.start_plan(MIXED)
    assert repository2.list_tasks() == []


def test_invalid_life_item_and_missing_graph_dependency_fail(tmp_path):
    repository, _, _, service = system(tmp_path, LifeRunner(bad_life_id=True))
    with pytest.raises(ValueError, match="invalid_life_item_id"):
        service.start_plan(LIFE_ONLY)
    assert repository.list_tasks() == []
    with pytest.raises(ValueError, match="missing_required_dependency"):
        validate_execution_plan(ExecutionPlan(intent="mixed", steps=[
            StepSpec(step_id="memory", agent="memory", purpose="memory"),
            StepSpec(step_id="learning", agent="learning", purpose="learning", depends_on=["memory"]),
            StepSpec(step_id="life", agent="life", purpose="life", depends_on=["memory"]),
            StepSpec(step_id="schedule", agent="schedule", purpose="schedule", depends_on=["life"]),
            StepSpec(step_id="task", agent="task", purpose="task", depends_on=["schedule"]),
        ]))


def test_v1_migration_keeps_five_user_configs_and_adds_life(tmp_path, monkeypatch):
    path = tmp_path / "old_v1.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        migrations.migrate_v1(connection)
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    repository = Repository(path)
    for role, definition in ROLE_DEFINITIONS.items():
        if role != "life":
            repository.insert_agent_config(role, definition.description,
                                           definition.instructions, set(definition.maximum_tools))
    repository.update_agent_config("learning", "my edited instruction", {"read_goals"})
    repository.create_run("old run")
    original = migrations.migrate_v2

    def injected_failure(connection):
        original(connection)
        raise RuntimeError("injected v2 failure")

    monkeypatch.setattr(migrations, "migrate_v2", injected_failure)
    with pytest.raises(RuntimeError, match="injected v2 failure"):
        repository.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_configs").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    monkeypatch.setattr(migrations, "migrate_v2", original)
    repository.initialize()
    backup = list(tmp_path.glob("old_v1.backup-v1-*.sqlite3"))
    assert backup
    with sqlite3.connect(backup[-1]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert repository.get_agent_config("learning")["instructions"] == "my edited instruction"
    assert repository.get_agent_config("learning")["version"] == 2
    assert len(repository.list_agent_configs()) == 5
    AgentRegistry(repository)
    assert {row["role"] for row in repository.list_agent_configs()} == set(ROLE_DEFINITIONS)
    assert repository.get_agent_config("life")["version"] == 1
    with repository.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION


def test_mixed_plan_is_visible_and_confirmable_in_existing_web_pages(tmp_path, monkeypatch):
    path = tmp_path / "life_ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    fake = LifeRunner()
    monkeypatch.setattr(AgnoAgentRunner, "run",
                        lambda self, role, payload, schema, tools: fake.run(role, payload, schema, tools))
    repository = Repository(path)
    repository.initialize()
    for begin, end in ((moment(10, 22), moment(11, 0)),
                       (moment(11, 4), moment(11, 5))):
        repository.create_time_block(TimeBlockCreate(kind="available", start_at=begin, end_at=end))
    at = AppTest.from_file(APP).run(timeout=30)
    at.text_area[0].set_value(MIXED)
    next(item for item in at.button if item.label == "生成计划").click().run(timeout=30)
    assert not at.exception
    run = repository.list_runs()[0]
    assert run["status"] == "waiting_approval"
    assert repository.list_tasks() == []
    assert [step["agent"] for step in repository.list_run_steps(run["id"])] == [
        "orchestrator", "memory", "learning", "life", "schedule", "task"
    ]
    assert any("life" in item.value for item in at.markdown)
    next(item for item in at.button if item.label == "确认并加入待办").click().run(timeout=30)
    assert not at.exception
    assert {task["domain"] for task in repository.list_tasks()} == {"study", "life"}
    at.switch_page("pages/03_agents.py").run(timeout=30)
    assert not at.exception
    assert any("生活智能体" in item.label for item in at.expander)
    at.switch_page("pages/04_trace.py").run(timeout=30)
    assert not at.exception


@pytest.mark.skipif(
    os.environ.get("RUN_DEEPSEEK_SMOKE") != "1" or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY for a real mixed DeepSeek run",
)
def test_real_deepseek_mixed_study_life_plan(tmp_path):
    config = load_config(require_model=True)
    zone = ZoneInfo(config.timezone)
    tomorrow = datetime.now(zone).date() + timedelta(days=1)
    repository = Repository(tmp_path / "real_mixed.sqlite3")
    repository.initialize()
    repository.create_time_block(TimeBlockCreate(
        kind="available", start_at=datetime.combine(tomorrow, time(13), zone),
        end_at=datetime.combine(tomorrow, time(19), zone),
    ))
    registry = AgentRegistry(repository)
    gateway = ToolGateway(repository, registry, config.timezone)
    service = PersonalAIService(
        repository, gateway,
        Harness(repository, registry, gateway, AgnoAgentRunner(config, registry), config),
    )
    request = (
        "明晚七点有 AI Agent 面试，请至少制定一项面试学习任务；我还想每周一、三、五运动，"
        "周末买菜，请至少提出一项运动或买菜的生活任务，合并为计划草案。"
        "明天下午一点到七点可用，请尽量安排在可用时间里。"
    )
    try:
        draft = service.start_plan(request)
    except ValueError:
        failed = repository.list_runs()[0]
        assert failed["status"] == "failed" and repository.list_tasks() == []
        draft = service.retry_run(failed["id"])
    assert [step["agent"] for step in repository.list_run_steps(draft.run_id)] == [
        "orchestrator", "memory", "learning", "life", "schedule", "task"
    ]
    assert all(step["status"] == "success" for step in repository.list_run_steps(draft.run_id))
    assert {task.domain for task in draft.tasks} == {"study", "life"}
    assert all(task.source_step_id in {"learning", "life"} for task in draft.tasks)
    assert repository.list_tasks() == []
    assert repository.get_run(draft.run_id)["status"] == "waiting_approval"
