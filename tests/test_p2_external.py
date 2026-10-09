"""No account or network needed: capability, approval and uncertain-result boundaries."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from personal_ai_os import migrations
from personal_ai_os.contracts import TaskCreate
from personal_ai_os.external import ExternalGateway, ExternalKnownFailure, ExternalUnknownResult
from personal_ai_os.storage import Repository


class CalendarFake:
    def __init__(self):
        self.calls = []
        self.mode = "success"

    def list_events(self, account_id, query):
        self.calls.append(("read", account_id, query))
        return [{"id": "event-1", "title": "Meeting"}]

    def create_event(self, account_id, calendar_id, event, idempotency_key):
        self.calls.append(("create", account_id, calendar_id, event, idempotency_key))
        if self.mode == "timeout":
            raise TimeoutError("network timeout")
        if self.mode == "failed":
            raise ExternalKnownFailure("not sent")
        if self.mode == "unknown":
            raise ExternalUnknownResult("connection lost after send")
        return {"event_id": "event-1"}

    def update_event(self, account_id, event_id, changes, idempotency_key):
        self.calls.append(("update", account_id, event_id, changes, idempotency_key))
        return {"event_id": event_id}


class MailFake:
    def __init__(self):
        self.calls = []

    def list_messages(self, account_id, query):
        self.calls.append(("read", account_id, query))
        return [{"id": "m1", "subject": "Hello"}]

    def send_message(self, account_id, recipient, message, idempotency_key):
        self.calls.append(("send", account_id, recipient, message, idempotency_key))
        return {"message_id": "m1"}


def ready(tmp_path, kind="calendar"):
    repo = Repository(tmp_path / "external.sqlite3")
    repo.initialize()
    gateway = ExternalGateway(repo)
    server = gateway.register_server("Personal account", kind)
    adapter = CalendarFake() if kind == "calendar" else MailFake()
    gateway.adapters[server["id"]] = adapter
    server = gateway.set_server_status(server["id"], server["version"], True)
    return repo, gateway, server, adapter


def allow(gateway, server, operation):
    return gateway.set_operation_enabled(server["id"], operation, server["version"], True)


def event():
    return {"title": "Meeting", "start_at_utc": "2026-10-10T10:00:00+00:00",
            "end_at_utc": "2026-10-10T11:00:00+00:00"}


def test_directory_allowlist_read_provenance_and_audit(tmp_path):
    repo, gateway, server, adapter = ready(tmp_path)
    with pytest.raises(PermissionError, match="external_operation_not_allowed"):
        gateway.read(server["id"], "list_events", "me@example.com", {})
    assert adapter.calls == []
    server = allow(gateway, server, "list_events")
    result = gateway.read(server["id"], "list_events", "me@example.com", {})
    assert result["source"] == {"server_id": server["id"], "operation": "list_events",
                                "account_id": "me@example.com"}
    assert result["items"][0]["id"] == "event-1"
    with pytest.raises(PermissionError):
        gateway.propose_write(server["id"], "create_event", "me@example.com", "main", event())
    assert len(adapter.calls) == 1
    server = gateway.set_server_status(server["id"], server["version"], False)
    with pytest.raises(PermissionError):
        gateway.read(server["id"], "list_events", "me@example.com", {})
    assert {item["event_type"] for item in gateway.list_audit()} >= {
        "server_registered", "server_status_changed", "operation_status_changed",
        "operation_denied", "read_succeeded",
    }
    reopened = ExternalGateway(Repository(repo.path))
    assert reopened.list_servers()[0]["status"] == "paused"
    gateway.set_server_status(server["id"], server["version"], True)
    with pytest.raises(RuntimeError, match="external_adapter_unavailable"):
        reopened.read(server["id"], "list_events", "me@example.com", {})


def test_calendar_preview_approval_idempotent_and_scope_recheck(tmp_path):
    repo, gateway, server, adapter = ready(tmp_path)
    server = allow(gateway, server, "create_event")
    proposal = gateway.propose_write(server["id"], "create_event", "me@example.com", "main", event())
    assert proposal["status"] == "pending" and proposal["payload"] == event()
    assert adapter.calls == []
    with pytest.raises(ValueError, match="external_credentials_not_allowed"):
        gateway.propose_write(server["id"], "create_event", "me@example.com", "main",
                              {**event(), "token": "secret"})
    server = gateway.set_operation_enabled(server["id"], "create_event", server["version"], False)
    with pytest.raises(PermissionError):
        gateway.approve_write(proposal["id"], 0)
    assert adapter.calls == [] and gateway.get_proposal(proposal["id"])["status"] == "pending"
    assert any(item["event_type"] == "write_approval_denied" for item in gateway.list_audit())
    server = allow(gateway, server, "create_event")
    result = gateway.approve_write(proposal["id"], 0)
    assert result["status"] == "succeeded" and result["revision"] == 1
    assert result["result"] == {"event_id": "event-1"}
    assert len(adapter.calls) == 1 and adapter.calls[0][-1]
    assert gateway.approve_write(proposal["id"], 0)["status"] == "succeeded"
    assert len(adapter.calls) == 1
    reopened = ExternalGateway(Repository(repo.path), {server["id"]: adapter})
    assert reopened.approve_write(proposal["id"], 0)["status"] == "succeeded"
    assert len(adapter.calls) == 1
    assert repo.list_tasks() == [] and repo.list_approved_memories() == []


@pytest.mark.parametrize("mode,status", [("timeout", "timeout"), ("failed", "failed"),
                                          ("unknown", "unknown")])
def test_failed_timeout_unknown_never_automatically_resent(tmp_path, mode, status):
    _, gateway, server, adapter = ready(tmp_path)
    server = allow(gateway, server, "create_event")
    proposal = gateway.propose_write(server["id"], "create_event", "me", "main", event())
    adapter.mode = mode
    result = gateway.approve_write(proposal["id"], 0)
    assert result["status"] == status and result["error_code"]
    if status in {"timeout", "unknown"}:
        assert gateway.propose_write(server["id"], "create_event", "me", "main", event())["id"] == proposal["id"]
    assert gateway.approve_write(proposal["id"], 0)["status"] == status
    with pytest.raises(ValueError):
        gateway.approve_write(proposal["id"], 1)
    assert len(adapter.calls) == 1


def test_mail_separate_read_send_and_item_review(tmp_path):
    _, gateway, server, adapter = ready(tmp_path, "mail")
    server = allow(gateway, server, "list_messages")
    assert gateway.read(server["id"], "list_messages", "user@example.com", {})["items"]
    with pytest.raises(PermissionError):
        gateway.propose_write(server["id"], "send_message", "user@example.com",
                              "friend@example.com", {"subject": "Hi", "body": "Hello"})
    server = allow(gateway, server, "send_message")
    first = gateway.propose_write(server["id"], "send_message", "user@example.com",
                                  "friend@example.com", {"subject": "Hi", "body": "Hello"})
    second = gateway.propose_write(server["id"], "send_message", "user@example.com",
                                   "other@example.com", {"subject": "Hi", "body": "Hello"})
    assert [call[0] for call in adapter.calls] == ["read"]
    gateway.reject_write(first["id"], 0)
    with pytest.raises(ValueError):
        gateway.approve_write(first["id"], 0)
    assert gateway.approve_write(second["id"], 0)["status"] == "succeeded"
    assert [call[0] for call in adapter.calls] == ["read", "send"]
    assert adapter.calls[-1][2] == "other@example.com"


def test_mcp_requires_both_directory_and_code_bound_capability(tmp_path):
    repo = Repository(tmp_path / "mcp.sqlite3")
    repo.initialize()
    gateway = ExternalGateway(repo)
    server = gateway.register_server("Mock MCP", "mcp", {"find_notes": "read"})
    server = gateway.set_server_status(server["id"], 1, True)
    server = allow(gateway, server, "find_notes")
    class MockMcp:
        operations = {"other": "read"}
        def read(self, operation, account_id, query):
            return [{"id": "n1"}]
    gateway.adapters[server["id"]] = MockMcp()
    with pytest.raises(PermissionError, match="external_code_capability_missing"):
        gateway.read(server["id"], "find_notes", "me", {})
    gateway.adapters[server["id"]].operations = {"find_notes": "read"}
    assert gateway.read(server["id"], "find_notes", "me", {})["items"] == [{"id": "n1"}]
    with pytest.raises(PermissionError):
        gateway.propose_write(server["id"], "find_notes", "me", "x", {})


def test_mcp_timeout_and_reviewed_write_are_audited(tmp_path):
    repo = Repository(tmp_path / "mcp-write.sqlite3")
    repo.initialize()
    gateway = ExternalGateway(repo)
    server = gateway.register_server("Mock MCP", "mcp",
                                     {"search": "read", "save": "write"})
    class MockMcp:
        operations = {"search": "read", "save": "write"}
        calls = []
        def read(self, operation, account_id, query):
            raise TimeoutError("simulated read timeout")
        def write(self, operation, account_id, target, payload, idempotency_key):
            self.calls.append((operation, account_id, target, payload, idempotency_key))
            return {"id": "saved-1"}
    adapter = MockMcp()
    gateway.adapters[server["id"]] = adapter
    server = gateway.set_server_status(server["id"], server["version"], True)
    server = allow(gateway, server, "search")
    server = allow(gateway, server, "save")
    with pytest.raises(TimeoutError):
        gateway.read(server["id"], "search", "me", {"q": "a"})
    proposal = gateway.propose_write(server["id"], "save", "me", "note-1", {"text": "hello"})
    assert adapter.calls == []
    assert gateway.approve_write(proposal["id"], 0)["status"] == "succeeded"
    assert len(adapter.calls) == 1
    assert {item["event_type"] for item in gateway.list_audit()} >= {
        "read_failed", "write_proposed", "write_attempt_started", "write_succeeded"
    }


def test_interrupted_attempt_recovered_as_unknown_no_resend(tmp_path):
    _, gateway, server, adapter = ready(tmp_path)
    server = allow(gateway, server, "update_event")
    proposal = gateway.propose_write(server["id"], "update_event", "me", "event-1", {"title": "New"})
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with gateway.repository.transaction() as conn:
        conn.execute("UPDATE external_write_proposals SET status='attempting',revision=1,"
                     "lease_until_utc=? WHERE id=?", (past, proposal["id"]))
    assert gateway.recover_expired_attempts() == 1
    assert gateway.get_proposal(proposal["id"])["status"] == "unknown"
    assert gateway.approve_write(proposal["id"], 0)["status"] == "unknown"
    assert adapter.calls == []


def test_v8_migration_backup_rollback(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    repo = Repository(path)
    repo.initialize()
    task = repo.create_task(TaskCreate(title="Preserved"))
    with sqlite3.connect(path) as conn:
        for table in ("external_audit", "external_write_proposals", "mcp_operations", "mcp_servers"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("PRAGMA user_version=8")
    original = migrations.migrate_v9
    def fail(connection):
        original(connection)
        raise RuntimeError("injected_v9_failure")
    monkeypatch.setattr(migrations, "migrate_v9", fail)
    with pytest.raises(RuntimeError, match="injected_v9_failure"):
        repo.initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='mcp_servers'").fetchone()[0] == 0
    assert list(tmp_path.glob("old.backup-v8-*.sqlite3"))
    monkeypatch.setattr(migrations, "migrate_v9", original)
    repo.initialize()
    assert repo.get_task(task["id"])["title"] == "Preserved"
    with repo.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION
