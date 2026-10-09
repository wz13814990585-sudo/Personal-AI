"""Credential-free YouTube MCP transport, permission, failure and planning tests."""

from dataclasses import replace
from pathlib import Path
import sqlite3
import sys

import pytest

from personal_ai_os.contracts import LearningPlan, TaskCreate
from personal_ai_os.storage import Repository
from personal_ai_os import migrations
from personal_ai_os.youtube_mcp import YouTubeMcpAdapter
from personal_ai_os import youtube_mcp_server as server_module

VIDEO_ID = "dQw4w9WgXcQ"
VIDEO = {"video_id": VIDEO_ID, "title": "Agent tutorial", "channel": "Public Channel",
         "url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
         "source": "YouTube Data API v3", "duration_seconds": 615,
         "retrieved_at_utc": "2026-10-09T00:00:00+00:00"}


class FakeYouTube:
    operations = {"search_videos": "read", "get_video_details": "read"}

    def __init__(self):
        self.calls = []
        self.failure = None

    def read(self, operation, account_id, query):
        self.calls.append((operation, account_id, query))
        if self.failure:
            raise self.failure
        return [{key: value for key, value in VIDEO.items() if key not in {"duration_seconds", "retrieved_at_utc"}}] if operation == "search_videos" else [VIDEO]


def setup_youtube(service):
    entry = service.register_youtube_mcp()
    fake = FakeYouTube()
    service.external.adapters[entry["id"]] = fake
    return entry, fake


def allow(service, entry):
    entry = service.set_external_server_status(entry["id"], entry["version"], True)
    for operation in ("search_videos", "get_video_details"):
        entry = service.set_external_operation_enabled(entry["id"], operation, entry["version"], True)
    return entry


def test_starts_paused_and_requires_both_read_permissions(flow_system):
    _, _, _, _, _, service = flow_system
    entry, fake = setup_youtube(service)
    assert entry["status"] == "paused" and all(not op["enabled"] for op in entry["operations"])
    with pytest.raises(PermissionError):
        service.search_youtube("Agent learning")
    entry = service.set_external_server_status(entry["id"], entry["version"], True)
    entry = service.set_external_operation_enabled(entry["id"], "search_videos", entry["version"], True)
    with pytest.raises(PermissionError):
        service.search_youtube("Agent learning")
    assert fake.calls == []
    entry = service.set_external_operation_enabled(entry["id"], "get_video_details", entry["version"], True)
    assert service.search_youtube("Agent learning")["items"] == [VIDEO]
    assert [call[0] for call in fake.calls] == ["search_videos", "get_video_details"]
    entry = service.set_external_server_status(entry["id"], entry["version"], False)
    with pytest.raises(PermissionError):
        service.search_youtube("Agent learning")
    assert len(fake.calls) == 2
    assert any(item["event_type"] == "operation_denied" for item in service.list_external_audit())
    with pytest.raises(ValueError, match="youtube_search_query_required"):
        service.start_plan("学习 Agent", use_youtube=True)
    assert service.list_runs() == []


def test_transport_rejects_missing_key_bad_output_and_timeout(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    adapter = YouTubeMcpAdapter(api_key="")
    with pytest.raises(RuntimeError, match="youtube_api_key_missing"):
        adapter.read("search_videos", "public", {"query": "python"})
    script = tmp_path / "bad_server.py"
    script.write_text("import sys\nsys.stdin.readline()\nprint('not json', flush=True)\n")
    bad = YouTubeMcpAdapter(command=[sys.executable, str(script)])
    with pytest.raises(ValueError, match="youtube_mcp_invalid_response"):
        bad.read("search_videos", "public", {"query": "python"})
    script.write_text("import sys,time\nsys.stdin.readline()\ntime.sleep(2)\n")
    slow = YouTubeMcpAdapter(timeout_seconds=0.1, command=[sys.executable, str(script)])
    with pytest.raises(TimeoutError, match="youtube_mcp_timeout"):
        slow.read("search_videos", "public", {"query": "python"})
    with pytest.raises(PermissionError):
        adapter.read("search_videos", "private", {"query": "python"})
    with pytest.raises(PermissionError):
        adapter.read("delete_video", "public", {})
    with pytest.raises(ValueError):
        adapter.read("get_video_details", "public", {"video_id": "bad"})


def test_handshake_rejects_wrong_server_before_tool_call(tmp_path):
    calls = tmp_path / "calls.txt"
    script = tmp_path / "wrong_server.py"
    script.write_text(
        "import json,sys\n"
        f"path={str(calls)!r}\n"
        "for line in sys.stdin:\n"
        " request=json.loads(line)\n"
        " with open(path,'a') as out: out.write(request.get('method','')+'\\n')\n"
        " if request.get('id') == 1:\n"
        "  print(json.dumps({'jsonrpc':'2.0','id':1,'result':{"
        "'protocolVersion':'2025-06-18','capabilities':{'tools':{}},"
        "'serverInfo':{'name':'impostor','version':'1.0'}}}),flush=True)\n"
    )
    adapter = YouTubeMcpAdapter(command=[sys.executable, str(script)])
    with pytest.raises(ValueError, match="youtube_mcp_invalid_response"):
        adapter.read("search_videos", "public", {"query": "agent"})
    assert calls.read_text().splitlines() == ["initialize"]


def test_server_only_public_metadata_and_quota_failure(monkeypatch):
    def fake_api(endpoint, params):
        if endpoint == "search":
            return {"items": [{"id": {"videoId": VIDEO_ID}, "snippet": {
                "title": "Agent tutorial", "channelTitle": "Public Channel"}}]}
        return {"items": [{"id": VIDEO_ID, "snippet": {"title": "Agent tutorial", "channelTitle": "Public Channel"},
                           "contentDetails": {"duration": "PT10M15S"},
                           "status": {"privacyStatus": "public"}}]}
    monkeypatch.setattr(server_module, "_api", fake_api)
    assert server_module.search_videos({"query": "Agent"})[0]["url"] == VIDEO["url"]
    assert server_module.get_video_details({"video_id": VIDEO_ID})[0]["duration_seconds"] == 615
    assert server_module._response({"id": 3, "method": "tools/call", "params": {
        "name": "delete_video", "arguments": {}}})["error"]["message"] == "youtube_operation_not_allowed"
    monkeypatch.setattr(server_module, "_api", lambda *_: (_ for _ in ()).throw(RuntimeError("youtube_quota_or_permission")))
    response = server_module._response({"id": 3, "method": "tools/call", "params": {
        "name": "search_videos", "arguments": {"query": "Agent"}}})
    assert response["error"]["message"] == "youtube_quota_or_permission"
    monkeypatch.setattr(server_module, "_api", lambda *_: {"items": [{"id": {"videoId": "bad"},
        "snippet": {"title": "Agent", "channelTitle": "Channel"}}]})
    bad = server_module._response({"id": 4, "method": "tools/call", "params": {
        "name": "search_videos", "arguments": {"query": "Agent"}}})
    assert bad["error"]["message"] == "youtube_invalid_video_id"


def test_search_budget_cache_and_restart(flow_system):
    repo, _, gateway, _, harness, service = flow_system
    entry, fake = setup_youtube(service)
    allow(service, entry)
    first = service.search_youtube("AI agents")
    assert not first["source"]["cache"]
    second = service.search_youtube("AI agents")
    assert second["source"]["cache"] and len(fake.calls) == 2
    from personal_ai_os.services import PersonalAIService
    restored = PersonalAIService(repo, gateway, harness)
    restored.external.adapters[entry["id"]] = fake
    assert restored.search_youtube("AI agents")["source"]["cache"]
    assert len(fake.calls) == 2
    for index in range(1, 10):
        service.search_youtube(f"query {index}")
    with pytest.raises(RuntimeError, match="youtube_local_search_budget_exceeded"):
        service.search_youtube("one more query")
    assert len(fake.calls) == 20


def test_opt_in_learning_draft_and_approval_preserve_resource(flow_system, monkeypatch):
    repo, _, _, runner, harness, service = flow_system
    entry, fake = setup_youtube(service)
    allow(service, entry)
    original_run = runner.run

    def run(role, payload, schema, tools):
        result = original_run(role, payload, schema, tools)
        if role == "learning" and "youtube_resources" in payload:
            assert payload["youtube_resources"] == [VIDEO]
            return LearningPlan(items=[result.items[0].model_copy(update={
                "resource_video_id": VIDEO_ID})], explanation="Watch public tutorial")
        return result
    monkeypatch.setattr(runner, "run", run)
    draft = service.start_plan("学习 AI Agent 面试", use_youtube=True, youtube_query="AI Agent interview")
    assert service.get_run(draft.run_id)["status"] == "waiting_approval"
    assert repo.list_tasks() == []
    assert draft.tasks[0].resource_url == VIDEO["url"]
    assert draft.tasks[0].resource_channel == "Public Channel"
    assert any("YouTube 资料" in text and VIDEO["url"] in text for text in draft.explanations)
    assert any(item["event_type"] == "youtube_resources_selected" for item in service.list_trace(draft.run_id))
    assert [__import__("json").loads(item["summary_json"])["operation"]
            for item in service.list_trace(draft.run_id) if item["event_type"] == "youtube_mcp_read"] == [
                "search_videos", "get_video_details"]
    with pytest.raises(ValueError, match="task_source_cannot_change"):
        service.edit_plan(draft.run_id, draft.revision, [
            draft.tasks[0].model_copy(update={"resource_url": "https://example.org/forged"})
        ])
    assert repo.list_tasks() == []
    approved = service.approve_plan(draft.run_id, draft.revision)
    assert approved[0]["resource_url"] == VIDEO["url"]
    assert service.approve_plan(draft.run_id, draft.revision)[0]["id"] == approved[0]["id"]
    assert len(fake.calls) == 2
    assert any(role == "learning" for role, _, _ in runner.calls)


def test_bad_resource_reference_or_failure_never_writes_tasks(flow_system, monkeypatch):
    repo, _, _, runner, _, service = flow_system
    entry, fake = setup_youtube(service)
    allow(service, entry)
    original_run = runner.run
    def forged(role, payload, schema, tools):
        result = original_run(role, payload, schema, tools)
        if role == "learning":
            return result.model_copy(update={"items": [result.items[0].model_copy(update={"resource_video_id": "AAAAAAAAAAA"})]})
        return result
    monkeypatch.setattr(runner, "run", forged)
    with pytest.raises(ValueError, match="invalid_youtube_resource_reference"):
        service.start_plan("学习 Agent", use_youtube=True, youtube_query="AI Agent")
    assert repo.list_tasks() == [] and service.list_runs()[0]["status"] == "failed"
    fake.failure = TimeoutError("youtube_mcp_timeout")
    with pytest.raises(TimeoutError):
        service.start_plan("学习 Python", use_youtube=True, youtube_query="Python")
    assert repo.list_tasks() == [] and service.list_runs()[0]["status"] == "failed"
    assert any(item["event_type"] == "youtube_resources_failed" for item in service.list_trace(service.list_runs()[0]["id"]))


def test_v11_resource_link_migration_backup_and_rollback(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    repo = Repository(path)
    repo.initialize()
    existing = repo.create_task(TaskCreate(title="Earlier task", estimated_minutes=30))
    with sqlite3.connect(path) as conn:
        for column in ("resource_url", "resource_source", "resource_channel", "resource_duration_seconds"):
            conn.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
        conn.execute("PRAGMA user_version=11")
    original = migrations.migrate_v12
    def fail(conn):
        original(conn)
        raise RuntimeError("injected_v12_failure")
    monkeypatch.setattr(migrations, "migrate_v12", fail)
    with pytest.raises(RuntimeError, match="injected_v12_failure"):
        repo.initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        assert "resource_url" not in {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert conn.execute("SELECT title FROM tasks WHERE id=?", (existing["id"],)).fetchone()[0] == "Earlier task"
    assert list(tmp_path.glob("old.backup-v11-*.sqlite3"))
    monkeypatch.setattr(migrations, "migrate_v12", original)
    repo.initialize()
    assert repo.get_task(existing["id"])["title"] == "Earlier task"
    assert "resource_url" in repo.get_task(existing["id"])
