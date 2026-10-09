"""Safe retry checkpoints, measured usage, optional prices and read-only export."""

import csv
import io
import json
import sqlite3
from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from agno.exceptions import ModelProviderError
from agno.models.deepseek import DeepSeek

from personal_ai_os import migrations
from personal_ai_os.contracts import GoalCreate, TaskCreate
from personal_ai_os.model_factory import make_deepseek_model
from personal_ai_os.storage import Repository, SCHEMA


REQUEST = "明晚七点有 AI Agent 面试，请制定学习计划并安排任务"


def test_failed_run_retry_reuses_only_validated_read_only_steps_and_requires_approval(flow_system, monkeypatch):
    repo, _, _, runner, harness, service = flow_system
    monkeypatch.setattr(harness, "_local_now", lambda: "2026-10-09T10:00:00+11:00")
    original = runner.run
    state = {"fail": True}
    def failing_schedule(role, payload, schema, tools):
        if role == "schedule" and state["fail"]:
            raise TimeoutError("temporary timeout")
        return original(role, payload, schema, tools)
    runner.run = failing_schedule
    with pytest.raises(TimeoutError):
        service.start_plan(REQUEST)
    failed = repo.list_runs()[0]
    assert failed["status"] == "failed" and repo.list_tasks() == []
    with repo.connection() as connection:
        checkpoints = connection.execute(
            "SELECT step_id FROM run_checkpoints WHERE run_id=? ORDER BY rowid", (failed["id"],)
        ).fetchall()
    assert [row[0] for row in checkpoints] == ["orchestrator", "memory", "learning"]
    assert len([event for event in repo.list_trace(failed["id"])
                if event["event_type"] == "step_attempt_retry"]) == 2
    state["fail"] = False
    runner.calls.clear()
    draft = service.retry_run(failed["id"])
    assert repo.get_run(failed["id"])["status"] == "failed"
    assert repo.get_run(draft.run_id)["retry_of_run_id"] == failed["id"]
    assert [role for role, _, _ in runner.calls] == ["schedule", "task"]
    assert len([event for event in repo.list_trace(draft.run_id)
                if event["event_type"] == "checkpoint_reused"]) == 3
    assert repo.list_tasks() == [] and repo.get_run(draft.run_id)["status"] == "waiting_approval"
    assert service.approve_plan(draft.run_id, draft.revision)
    assert len(repo.list_tasks()) == 1


def test_changed_read_state_or_role_configuration_prevents_checkpoint_reuse(flow_system, monkeypatch):
    repo, registry, _, runner, harness, service = flow_system
    monkeypatch.setattr(harness, "_local_now", lambda: "2026-10-09T10:00:00+11:00")
    original = runner.run
    state = {"fail": True}
    def failing_schedule(role, payload, schema, tools):
        if role == "schedule" and state["fail"]:
            raise TimeoutError("temporary timeout")
        return original(role, payload, schema, tools)
    runner.run = failing_schedule
    with pytest.raises(TimeoutError):
        service.start_plan(REQUEST)
    failed_id = repo.list_runs()[0]["id"]
    repo.create_goal(GoalCreate(title="新增目标"))
    state["fail"] = False
    runner.calls.clear()
    retry = service.retry_run(failed_id)
    assert [role for role, _, _ in runner.calls] == [
        "orchestrator", "memory", "learning", "schedule", "task"
    ]
    assert not [event for event in repo.list_trace(retry.run_id)
                if event["event_type"] == "checkpoint_reused"]
    role = registry.get_role("learning")
    registry.update_role_instruction("learning", role["instructions"] + " 新规则", set(role["tool_subset"]))
    with pytest.raises(ValueError, match="only_failed_runs_can_retry"):
        service.retry_run(retry.run_id)


def test_corrupt_checkpoint_is_rejected_and_step_runs_again(flow_system, monkeypatch):
    repo, _, _, runner, harness, service = flow_system
    monkeypatch.setattr(harness, "_local_now", lambda: "2026-10-09T10:00:00+11:00")
    original = runner.run
    state = {"fail": True}
    def schedule_failure(role, payload, schema, tools):
        if role == "schedule" and state["fail"]:
            raise TimeoutError("temporary timeout")
        return original(role, payload, schema, tools)
    runner.run = schedule_failure
    with pytest.raises(TimeoutError):
        service.start_plan(REQUEST)
    source = repo.list_runs()[0]
    with repo.transaction() as connection:
        connection.execute(
            "UPDATE run_checkpoints SET output_json='{}' WHERE run_id=? AND step_id='memory'",
            (source["id"],),
        )
    state["fail"] = False
    runner.calls.clear()
    retry = service.retry_run(source["id"])
    assert [role for role, _, _ in runner.calls] == ["memory", "schedule", "task"]
    assert any(event["event_type"] == "checkpoint_rejected"
               for event in repo.list_trace(retry.run_id))
    assert len([event for event in repo.list_trace(retry.run_id)
                if event["event_type"] == "checkpoint_reused"]) == 2


def test_transient_rate_limit_retries_but_invalid_output_does_not(flow_system, monkeypatch):
    repo, _, _, runner, harness, service = flow_system
    monkeypatch.setattr(harness, "_local_now", lambda: "2026-10-09T10:00:00+11:00")
    original = runner.run
    calls = {"schedule": 0}
    def rate_limited(role, payload, schema, tools):
        if role == "schedule":
            calls["schedule"] += 1
            if calls["schedule"] < 3:
                raise ModelProviderError("rate limit", status_code=429)
        return original(role, payload, schema, tools)
    runner.run = rate_limited
    draft = service.start_plan(REQUEST)
    assert calls["schedule"] == 3
    assert len([event for event in repo.list_trace(draft.run_id)
                if event["event_type"] == "step_attempt_retry"]) == 2

    def invalid(role, payload, schema, tools):
        if role == "learning":
            calls["learning"] = calls.get("learning", 0) + 1
            raise ValueError("invalid JSON")
        return original(role, payload, schema, tools)
    runner.run = invalid
    with pytest.raises(ValueError, match="invalid JSON"):
        service.start_plan(REQUEST)
    assert calls["learning"] == 1


def test_failed_feedback_retry_keeps_original_and_still_requires_memory_review(flow_system):
    repo, _, _, runner, _, service = flow_system
    task = service.create_task(TaskCreate(title="复习面试"))
    service.complete_task(task["id"])
    runner.fail_feedback = True
    with pytest.raises(TimeoutError):
        service.submit_feedback(task["id"], "不要在早上安排学习")
    source = next(row for row in repo.list_runs() if row["kind"] == "feedback")
    assert source["status"] == "failed"
    original_feedback = repo.feedback_for_run(source["id"])
    runner.fail_feedback = False
    proposals = service.retry_run(source["id"])
    assert len(proposals) == 1
    retry = next(row for row in repo.list_runs() if row["retry_of_run_id"] == source["id"])
    assert retry["status"] == "waiting_approval"
    assert repo.feedback_for_run(retry["id"])["id"] != original_feedback["id"]
    assert repo.get_run(source["id"])["status"] == "failed"
    assert repo.list_approved_memories() == []
    service.resolve_memory(proposals[0]["id"], approve=True)
    assert len(repo.list_approved_memories()) == 1


def test_metered_deepseek_counts_each_provider_request_and_missing_usage(monkeypatch):
    from personal_ai_os.config import load_config
    config = load_config(environ={"DEEPSEEK_API_KEY": "test-key"})
    model = make_deepseek_model(config)
    responses = [SimpleNamespace(response_usage=SimpleNamespace(
        input_tokens=120, output_tokens=30, total_tokens=150)),
        SimpleNamespace(response_usage=None)]
    monkeypatch.setattr(DeepSeek, "invoke", lambda self, *args, **kwargs: responses.pop(0))
    model.invoke([], None)
    model.invoke([], None)
    assert len(model.request_usage) == 2
    assert model.request_usage[0]["total_tokens"] == 150
    assert model.request_usage[1]["input_tokens"] is None
    assert all(item["model_id"] == "deepseek-flash" for item in model.request_usage)


def test_usage_prices_and_export_are_read_only_and_exclude_secrets(flow_system):
    repo, _, _, _, _, service = flow_system
    run = repo.create_run("usage example")
    repo.record_model_usage(run["id"], "orchestrator", 1, [
        {"model_id": "deepseek-flash", "input_tokens": 1_000_000,
         "output_tokens": 500_000, "total_tokens": 1_500_000, "duration_ms": 120},
        {"model_id": "deepseek-flash", "input_tokens": None,
         "output_tokens": None, "total_tokens": None, "duration_ms": 80},
    ], None)
    summary = service.usage_summary()
    assert summary["request_count"] == 2 and summary["duration_ms"] == 200
    assert summary["missing_token_records"] == 1
    assert summary["estimated_cost_usd"] is None
    service.set_model_prices("1.5", "2")
    summary = service.usage_summary()
    assert Decimal(summary["estimated_cost_usd"]) == Decimal("2.50")
    assert summary["estimated_request_count"] == 1
    with pytest.raises(ValueError, match="invalid_model_price"):
        service.set_model_prices("NaN", "2")
    repo.set_setting("DEEPSEEK_API_KEY", "secret-do-not-export")
    repo.create_goal(GoalCreate(title="学习目标"))
    repo.create_task(TaskCreate(title="学习任务"))
    before = len(repo.list_tasks()), len(repo.list_trace())
    exported = service.export_data("json")
    parsed = json.loads(exported)
    assert parsed["goals"][0]["title"] == "学习目标"
    assert parsed["tasks"][0]["title"] == "学习任务"
    assert all(key in parsed for key in ("recurrence_rules", "habits", "feedback", "memories"))
    assert b"secret-do-not-export" not in exported and b"DEEPSEEK_API_KEY" not in exported
    assert b"model_prices" not in exported
    task_rows = list(csv.DictReader(io.StringIO(service.export_data("tasks_csv").decode())))
    assert task_rows[0]["title"] == "学习任务"
    with pytest.raises(ValueError, match="unsupported_export_format"):
        service.export_data("html")
    assert before == (len(repo.list_tasks()), len(repo.list_trace()))


def test_v4_to_v5_failure_rolls_back_without_losing_user_data(tmp_path, monkeypatch):
    path = tmp_path / "old_v4.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        for migrate in (migrations.migrate_v1, migrations.migrate_v2,
                        migrations.migrate_v3, migrations.migrate_v4):
            migrate(connection)
        connection.execute("PRAGMA user_version=4")
        connection.commit()
    repo = Repository(path)
    task = repo.create_task(TaskCreate(title="旧任务"))
    original = migrations.migrate_v5
    def fail_after_ddl(connection):
        original(connection)
        raise RuntimeError("injected v5 failure")
    monkeypatch.setattr(migrations, "migrate_v5", fail_after_ddl)
    with pytest.raises(RuntimeError, match="injected v5 failure"):
        repo.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='model_usage'").fetchone() is None
    assert repo.get_task(task["id"])["title"] == "旧任务"
    monkeypatch.setattr(migrations, "migrate_v5", original)
    repo.initialize()
    assert list(tmp_path.glob("old_v4.backup-v4-*.sqlite3"))
    with repo.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION
