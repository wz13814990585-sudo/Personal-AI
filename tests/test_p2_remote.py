"""Device security, local read-only view and durable opt-in delivery."""

import hashlib
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from personal_ai_os import migrations
from personal_ai_os.contracts import TaskCreate
from personal_ai_os.remote_access import DeviceAccess
from personal_ai_os.remote_delivery import (
    RemoteDeliveryDispatcher, RemoteNotificationKnownFailure,
)
from personal_ai_os.remote_view import RemoteViewApp
from personal_ai_os.storage import Repository
from personal_ai_os.worker import Worker


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self, mode="success"):
        self.mode = mode
        self.calls = []

    def send(self, device_id, title, body, idempotency_key):
        self.calls.append((device_id, title, body, idempotency_key))
        if self.mode == "known_failure":
            raise RemoteNotificationKnownFailure("not sent")
        if self.mode == "timeout":
            raise TimeoutError("result not known")
        if self.mode == "unknown":
            raise OSError("connection lost")


def notice(repo, *, status="delivered", at=NOW):
    job_id, notice_id = uuid4().hex, uuid4().hex
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO scheduled_jobs(id,kind,dedupe_key,payload_json,due_at_utc,status,"
            "created_at_utc,updated_at_utc) VALUES (?,'reminder',?,'{}',?,'completed',?,?)",
            (job_id, job_id, at.isoformat(), at.isoformat(), at.isoformat()),
        )
        conn.execute(
            "INSERT INTO notifications(id,job_id,title,body,original_due_at_utc,"
            "visible_at_utc,status,system_status) VALUES (?,?,?,?,?,?,?,'disabled')",
            (notice_id, job_id, "Study", "Start soon", at.isoformat(), at.isoformat(), status),
        )
    return notice_id


def call(app, method, path, *, form=None, cookie=""):
    body = urlencode(form or {}).encode()
    status_and_headers = []
    def start_response(status, headers):
        status_and_headers.append((status, dict(headers)))
    output = b"".join(app({
        "REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body), "HTTP_COOKIE": cookie,
    }, start_response))
    return status_and_headers[0][0], status_and_headers[0][1], output


def test_device_hash_session_expiry_revocation_and_read_only_snapshot(tmp_path):
    repo = Repository(tmp_path / "devices.sqlite3")
    repo.initialize()
    task = repo.create_task(TaskCreate(title="Private study"))
    access = DeviceAccess(repo)
    issued = access.create_device("Phone", now=NOW)
    device_id, token = issued["device"]["id"], issued["token"]
    assert issued["device"]["notifications_enabled"] == 0
    assert "token_hash" not in issued["device"]
    with repo.connection() as conn:
        stored = conn.execute("SELECT token_hash FROM remote_devices WHERE id=?", (device_id,)).fetchone()[0]
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(access.list_devices())
    assert token not in str(repo.list_trace())
    with pytest.raises(PermissionError):
        access.open_session("bad", now=NOW)
    session = access.open_session(token, now=NOW)
    assert task["id"] in [item["id"] for item in access.snapshot(session, now=NOW)["tasks"]]
    with pytest.raises(PermissionError):
        access.snapshot("bad", now=NOW)
    with pytest.raises(PermissionError):
        access.snapshot(session, now=NOW + timedelta(hours=13))
    with pytest.raises(ValueError, match="stale_or_revoked_device"):
        access.revoke_device(device_id, 999, now=NOW)
    access.revoke_device(device_id, 1, now=NOW)
    with pytest.raises(PermissionError):
        access.snapshot(session, now=NOW)
    with pytest.raises(PermissionError):
        access.open_session(token, now=NOW)
    assert repo.get_task(task["id"])["title"] == "Private study"


def test_protected_wsgi_view_escapes_content_and_has_no_writes(flow_system):
    repo, _, _, _, _, service = flow_system
    repo.create_task(TaskCreate(title="<script>alert(1)</script>"))
    app = RemoteViewApp(service)
    status, headers, body = call(app, "GET", "/dashboard")
    assert status.startswith("401") and b"script" not in body
    status, headers, body = call(app, "GET", "/api/overview")
    assert status.startswith("401") and b"script" not in body
    status, headers, body = call(app, "GET", "/")
    assert status.startswith("200") and b"script" not in body
    issued = service.create_remote_device("Tablet")
    status, headers, _ = call(app, "POST", "/session", form={"token": "wrong"})
    assert status.startswith("401")
    status, headers, _ = call(app, "POST", "/session", form={"token": issued["token"]})
    assert status.startswith("303") and "HttpOnly" in headers["Set-Cookie"]
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    status, headers, body = call(app, "GET", "/dashboard", cookie=cookie)
    assert status.startswith("200") and b"&lt;script&gt;" in body and b"<script>" not in body
    assert headers["Cache-Control"] == "no-store"
    status, headers, body = call(app, "GET", "/api/overview", cookie=cookie)
    assert status.startswith("200") and json.loads(body)["tasks"][0]["title"] == "<script>alert(1)</script>"
    status, _, _ = call(app, "POST", "/tasks", form={"title": "write"}, cookie=cookie)
    assert status.startswith("404")
    service.revoke_remote_device(issued["device"]["id"], issued["device"]["version"])
    status, _, body = call(app, "GET", "/dashboard", cookie=cookie)
    assert status.startswith("401") and b"script" not in body


@pytest.mark.parametrize("mode,expected", [
    ("success", "delivered"), ("known_failure", "failed"),
    ("timeout", "unknown"), ("unknown", "unknown"),
])
def test_opt_in_delivery_dedupe_restart_and_outcomes(tmp_path, mode, expected):
    repo = Repository(tmp_path / "delivery.sqlite3")
    repo.initialize()
    access = DeviceAccess(repo)
    device = access.create_device("Phone", now=NOW - timedelta(minutes=2))["device"]
    before = notice(repo, at=NOW - timedelta(minutes=1))
    fake = FakeNotifier(mode)
    dispatcher = RemoteDeliveryDispatcher(repo, fake)
    assert dispatcher.dispatch(NOW) == 0 and dispatcher.list_deliveries() == []
    access.set_notifications(device["id"], 1, True, now=NOW)
    after = notice(repo, at=NOW + timedelta(seconds=1))
    assert dispatcher.dispatch(NOW + timedelta(seconds=2)) == 1
    row = dispatcher.list_deliveries()[0]
    assert row["notification_id"] == after and row["status"] == expected
    assert row["notification_id"] != before and len(fake.calls) == 1
    reopened = Repository(repo.path)
    reopened.initialize()
    assert RemoteDeliveryDispatcher(reopened, fake).dispatch(NOW + timedelta(minutes=1)) == 0
    assert len(fake.calls) == 1
    assert {item["event_type"] for item in repo.list_trace()} >= {"remote_delivery_" + expected}


def test_missed_revoked_and_interrupted_attempt_never_send(tmp_path):
    repo = Repository(tmp_path / "interrupt.sqlite3")
    repo.initialize()
    access = DeviceAccess(repo)
    device = access.create_device("Phone", now=NOW)["device"]
    access.set_notifications(device["id"], 1, True, now=NOW)
    missed = notice(repo, status="missed", at=NOW + timedelta(seconds=1))
    fake = FakeNotifier()
    dispatcher = RemoteDeliveryDispatcher(repo, fake)
    assert dispatcher.dispatch(NOW + timedelta(seconds=2)) == 0
    assert dispatcher.list_deliveries()[0]["status"] == "missed"
    pending_notice = notice(repo, at=NOW + timedelta(seconds=3))
    # Simulate a process crash after the durable attempt marker and before outcome.
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO remote_deliveries(id,notification_id,device_id,status,idempotency_key,"
            "lease_until_utc,created_at_utc,updated_at_utc) VALUES (?,?,?,'attempting',?,?,?,?)",
            (uuid4().hex, pending_notice, device["id"], uuid4().hex,
             (NOW - timedelta(seconds=1)).isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
    assert dispatcher.dispatch(NOW + timedelta(seconds=4)) == 0
    assert {row["status"] for row in dispatcher.list_deliveries()} == {"missed", "unknown"}
    assert fake.calls == []
    new_notice = notice(repo, at=NOW + timedelta(seconds=5))
    dispatcher.dispatch(NOW + timedelta(seconds=6), max_items=0)
    assert any(row["notification_id"] == new_notice and row["status"] == "pending"
               for row in dispatcher.list_deliveries())
    access.revoke_device(device["id"], 2, now=NOW + timedelta(seconds=7))
    assert any(row["notification_id"] == new_notice and row["status"] == "cancelled"
               for row in dispatcher.list_deliveries())
    assert dispatcher.dispatch(NOW + timedelta(seconds=8)) == 0 and fake.calls == []


def test_v9_to_v10_backup_and_rollback(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    repo = Repository(path)
    repo.initialize()
    task = repo.create_task(TaskCreate(title="Keep task"))
    with sqlite3.connect(path) as conn:
        for table in ("remote_deliveries", "remote_sessions", "remote_devices"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("PRAGMA user_version=9")
    original = migrations.migrate_v10
    def fail(conn):
        original(conn)
        raise RuntimeError("injected_v10_failure")
    monkeypatch.setattr(migrations, "migrate_v10", fail)
    with pytest.raises(RuntimeError, match="injected_v10_failure"):
        repo.initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='remote_devices'").fetchone()[0] == 0
    assert list(tmp_path.glob("old.backup-v9-*.sqlite3"))
    monkeypatch.setattr(migrations, "migrate_v10", original)
    repo.initialize()
    assert repo.get_task(task["id"])["title"] == "Keep task"
    with repo.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION


def test_remote_notification_requires_bound_channel(flow_system):
    _, _, _, _, _, service = flow_system
    issued = service.create_remote_device("Phone")
    with pytest.raises(RuntimeError, match="remote_notification_channel_unavailable"):
        service.set_remote_device_notifications(issued["device"]["id"], 1, True)
    assert service.list_remote_devices()[0]["notifications_enabled"] == 0


def test_worker_dispatches_without_browser_and_restarted_worker_dedupes(tmp_path):
    repo = Repository(tmp_path / "worker-remote.sqlite3")
    repo.initialize()
    access = DeviceAccess(repo)
    device = access.create_device("Phone", now=NOW)["device"]
    access.set_notifications(device["id"], 1, True, now=NOW)
    notification_id = notice(repo, at=NOW + timedelta(seconds=1))
    fake = FakeNotifier()
    # The worker is independent of Streamlit and consumes persisted local notices.
    Worker(repo, remote_notifier=fake).run_once(NOW + timedelta(seconds=2), max_jobs=0)
    assert len(fake.calls) == 1
    assert RemoteDeliveryDispatcher(repo, fake).list_deliveries()[0]["notification_id"] == notification_id
    reopened = Repository(repo.path)
    reopened.initialize()
    Worker(reopened, remote_notifier=fake).run_once(NOW + timedelta(seconds=3), max_jobs=0)
    assert len(fake.calls) == 1


def test_remote_view_command_binds_loopback_only(flow_system, monkeypatch):
    import sys
    import wsgiref.simple_server

    from personal_ai_os import __main__ as entry

    repo, _, _, _, _, _ = flow_system
    captured = {}
    class FakeServer:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def serve_forever(self):
            captured["served"] = True
    def make_server(host, port, app):
        captured.update(host=host, port=port, app=app)
        return FakeServer()
    monkeypatch.setattr(sys, "argv", ["personal_ai_os", "remote-view", "--port", "8765"])
    monkeypatch.setattr(wsgiref.simple_server, "make_server", make_server)
    monkeypatch.setenv("DATABASE_PATH", str(repo.path))
    assert entry.main() == 0
    assert captured["host"] == "127.0.0.1" and captured["port"] == 8765
    assert isinstance(captured["app"], RemoteViewApp) and captured["served"]
