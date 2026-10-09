"""Mobile browser routes: device identity, CSRF, revision and approval boundaries."""

import io
import re
import http.client
import sys
import threading
from datetime import datetime, timedelta, time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest

from personal_ai_os.contracts import TaskCreate, TimeBlockCreate
from personal_ai_os.mobile_view import MobileViewApp
from test_p2_external import CalendarFake, event

ORIGIN = "https://mac.example.ts.net"
HOST = "mac.example.ts.net"


def call(app, method, path, *, form=None, cookie="", origin=ORIGIN, host=HOST, ip="100.64.1.1"):
    body = urlencode(form or {}).encode()
    output = []
    def start_response(status, headers):
        output.append((status, dict(headers)))
    content = b"".join(app({"REQUEST_METHOD": method, "PATH_INFO": path,
                            "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
                            "HTTP_COOKIE": cookie, "HTTP_ORIGIN": origin,
                            "HTTP_HOST": host, "REMOTE_ADDR": ip}, start_response))
    return output[0][0], output[0][1], content.decode()


def login(service, app):
    issued = service.create_remote_device("iPhone")
    status, headers, _ = call(app, "POST", "/session", form={"token": issued["token"]})
    assert status.startswith("303")
    assert all(part in headers["Set-Cookie"] for part in ("Secure", "HttpOnly", "SameSite=Strict"))
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    return issued, cookie


def csrf(app, cookie, path="/dashboard"):
    status, _, body = call(app, "GET", path, cookie=cookie)
    assert status.startswith("200")
    token = re.search(r"name='csrf' value='([0-9a-f]+)'", body)
    assert token
    return token.group(1), body


def test_auth_host_origin_csrf_expiry_and_revoke(flow_system):
    repo, _, _, _, _, service = flow_system
    app = MobileViewApp(service, public_origin=ORIGIN)
    assert call(app, "GET", "/dashboard")[0].startswith("401")
    assert call(app, "GET", "/dashboard", host="evil.test")[0].startswith("403")
    assert call(app, "POST", "/session", form={"token": "bad"}, origin="https://evil.test")[0].startswith("403")
    issued, cookie = login(service, app)
    token, body = csrf(app, cookie)
    assert "今日与审批" in body
    task = service.create_task(TaskCreate(title="Study"))
    assert call(app, "POST", f"/tasks/{task['id']}/edit", cookie=cookie,
                form={"version": task["version"], "title": "Malicious"})[0].startswith("403")
    assert call(app, "POST", f"/tasks/{task['id']}/edit", cookie=cookie, origin="https://evil.test",
                form={"csrf": token, "version": task["version"], "title": "Malicious"})[0].startswith("403")
    assert call(app, "POST", f"/tasks/{task['id']}/edit", cookie=cookie, origin="null",
                form={"csrf": token, "version": task["version"], "title": "Malicious"})[0].startswith("403")
    assert repo.get_task(task["id"])["title"] == "Study"
    with repo.transaction() as conn:
        conn.execute("UPDATE remote_sessions SET expires_at_utc='2000-01-01T00:00:00+00:00'")
    assert call(app, "GET", "/dashboard", cookie=cookie)[0].startswith("401")
    issued2, cookie2 = login(service, app)
    assert csrf(app, cookie2)[0]
    service.revoke_remote_device(issued2["device"]["id"], issued2["device"]["version"])
    assert call(app, "GET", "/dashboard", cookie=cookie2)[0].startswith("401")
    assert call(app, "POST", "/session", form={"token": issued2["token"]})[0].startswith("401")
    assert all(row["id"] != issued2["device"]["id"] or row["status"] == "revoked" for row in service.list_remote_devices())


def test_task_edit_version_and_escaped_mobile_html(flow_system):
    repo, _, _, _, _, service = flow_system
    task = service.create_task(TaskCreate(title="<script>alert(1)</script>"))
    app = MobileViewApp(service, public_origin=ORIGIN)
    _, cookie = login(service, app)
    token, body = csrf(app, cookie, f"/tasks/{task['id']}")
    assert "&lt;script&gt;" in body and "<script>" not in body
    assert "当前状态：待处理" in body
    assert "<select name='priority'>" in body and ">高</option>" in body
    form = {"csrf": token, "version": str(task["version"]), "title": "Updated on iPhone",
            "priority": "high", "estimated_minutes": "45", "due_at_utc": "",
            "start_at_utc": "", "end_at_utc": ""}
    assert call(app, "POST", f"/tasks/{task['id']}/edit", cookie=cookie, form=form)[0].startswith("303")
    assert repo.get_task(task["id"])["title"] == "Updated on iPhone"
    assert call(app, "POST", f"/tasks/{task['id']}/edit", cookie=cookie, form=form)[0].startswith("409")
    assert repo.get_task(task["id"])["version"] == task["version"] + 1
    assert call(app, "POST", f"/tasks/{task['id']}/edit", cookie=cookie,
                form={**form, "version": str(task["version"] + 1), "start_at_utc": "2026-10-10T10:00"})[0].startswith("400")


def test_plan_edit_revision_approval_and_repeat_are_transactional(flow_system):
    repo, _, _, _, _, service = flow_system
    draft = service.start_plan("明晚七点有 AI Agent 面试，请安排学习")
    app = MobileViewApp(service, public_origin=ORIGIN)
    _, cookie = login(service, app)
    token, body = csrf(app, cookie, f"/plans/{draft.run_id}")
    assert "Review AI Agents" in body and "版本：0" in body
    edit = {"csrf": token, "revision": "0", "item_id": "review", "title": "Edited interview prep",
            "priority": "high", "estimated_minutes": "60",
            "start_at": draft.tasks[0].start_at.isoformat(), "end_at": draft.tasks[0].end_at.isoformat()}
    assert call(app, "POST", f"/plans/{draft.run_id}/edit", cookie=cookie, form=edit)[0].startswith("303")
    assert service.get_plan(draft.run_id).revision == 1
    assert repo.list_tasks() == []
    assert call(app, "POST", f"/plans/{draft.run_id}/approve", cookie=cookie,
                form={"csrf": token, "revision": "0"})[0].startswith("409")
    assert repo.list_tasks() == []
    approve = {"csrf": token, "revision": "1"}
    assert call(app, "POST", f"/plans/{draft.run_id}/approve", cookie=cookie,
                form=approve)[0].startswith("303")
    assert len(repo.list_tasks()) == 1 and repo.list_tasks()[0]["title"] == "Edited interview prep"
    assert call(app, "POST", f"/plans/{draft.run_id}/approve", cookie=cookie,
                form=approve)[0].startswith("303")
    assert len(repo.list_tasks()) == 1


def test_memory_review_and_external_write_preview_unknown_is_not_resent(flow_system):
    repo, _, _, _, _, service = flow_system
    task = service.create_task(TaskCreate(title="Done study"))
    service.complete_task(task["id"])
    _, proposals = service.submit_feedback(task["id"], "不要在早上安排学习")
    memory_id = proposals[0]["id"]
    server = service.register_external_server("Test calendar", "calendar")
    fake = CalendarFake()
    service.external.adapters[server["id"]] = fake
    server = service.set_external_server_status(server["id"], server["version"], True)
    server = service.set_external_operation_enabled(server["id"], "create_event", server["version"], True)
    repo.set_setting("icloud_calendar_selection", {"server_id": server["id"],
                     "calendar_id": "calendar", "title": "个人"})
    proposal = service.propose_external_write(server["id"], "create_event", "icloud", "calendar",
                                              {**event(), "reminder_minutes": 30})
    fake.mode = "unknown"
    app = MobileViewApp(service, public_origin=ORIGIN)
    _, cookie = login(service, app)
    token, memory_page = csrf(app, cookie, f"/memory/{memory_id}")
    assert "候选记忆" in memory_page and "不要在早上安排学习" in memory_page
    assert call(app, "POST", f"/memory/{memory_id}/approve", cookie=cookie,
                form={"csrf": token})[0].startswith("303")
    assert len(service.list_memories()) == 1
    assert call(app, "POST", f"/memory/{memory_id}/approve", cookie=cookie,
                form={"csrf": token})[0].startswith("409")
    _, external_page = csrf(app, cookie, f"/external/{proposal['id']}")
    assert "Meeting" in external_page and "当前 iCloud 日历：个人" in external_page
    assert "当地时间：" in external_page and "Australia/Sydney" in external_page
    assert "提前 30 分钟" in external_page
    write = {"csrf": token, "revision": "0"}
    assert call(app, "POST", f"/external/{proposal['id']}/approve", cookie=cookie,
                form=write)[0].startswith("303")
    assert service.list_external_write_proposals()[0]["status"] == "unknown"
    assert len(fake.calls) == 1
    assert call(app, "POST", f"/external/{proposal['id']}/approve", cookie=cookie,
                form=write)[0].startswith("303")
    assert len(fake.calls) == 1


def test_login_rate_limit_and_private_origin_validation(flow_system):
    _, _, _, _, _, service = flow_system
    with pytest.raises(ValueError):
        MobileViewApp(service, public_origin="http://public.example.test")
    app = MobileViewApp(service, public_origin=ORIGIN)
    results = [call(app, "POST", "/session", form={"token": "bad"})[0] for _ in range(11)]
    assert results[:10] == ["401 Unauthorized"] * 10
    assert results[10] == "429 Too Many Requests"


def test_daily_plan_approval_and_stale_baseline(flow_system):
    repo, _, _, _, _, service = flow_system
    zone = ZoneInfo(service.timezone)
    day = datetime.now(zone).date() + timedelta(days=1)
    repo.create_time_block(TimeBlockCreate(
        kind="available", start_at=datetime.combine(day, time(9), zone),
        end_at=datetime.combine(day, time(12), zone)))
    task = service.create_task(TaskCreate(title="Daily study", estimated_minutes=30))
    draft = service.propose_daily_plan(day, as_of=datetime.combine(day, time(8), zone))
    assert draft.status == "draft" and draft.actions
    app = MobileViewApp(service, public_origin=ORIGIN)
    _, cookie = login(service, app)
    token, page = csrf(app, cookie, f"/daily/{draft.id}")
    assert "Daily study" in page and "新时间" in page and "保存这项每日草案修改" in page
    assert repo.get_task(task["id"])["start_at_utc"] is None
    form = {"csrf": token, "revision": str(draft.revision)}
    action = draft.actions[0]
    edit = {**form, "action_id": action.action_id,
            "new_start_at": datetime.combine(day, time(10), zone).isoformat(),
            "new_end_at": datetime.combine(day, time(10, 30), zone).isoformat(),
            "reason": "手机调整到十点"}
    assert call(app, "POST", f"/daily/{draft.id}/edit", cookie=cookie, form=edit)[0].startswith("303")
    assert service.get_daily_plan(draft.id).revision == 1
    assert repo.get_task(task["id"])["start_at_utc"] is None
    assert call(app, "POST", f"/daily/{draft.id}/approve", cookie=cookie, form=form)[0].startswith("409")
    form["revision"] = "1"
    assert call(app, "POST", f"/daily/{draft.id}/approve", cookie=cookie, form=form)[0].startswith("303")
    assert datetime.fromisoformat(repo.get_task(task["id"])["start_at_utc"]) == datetime.fromisoformat(edit["new_start_at"])
    assert call(app, "POST", f"/daily/{draft.id}/approve", cookie=cookie, form=form)[0].startswith("303")
    assert len(repo.list_tasks()) == 1


def test_daily_plan_rejects_changed_task_baseline(flow_system):
    repo, _, _, _, _, service = flow_system
    zone = ZoneInfo(service.timezone)
    day = datetime.now(zone).date() + timedelta(days=1)
    repo.create_time_block(TimeBlockCreate(
        kind="available", start_at=datetime.combine(day, time(9), zone),
        end_at=datetime.combine(day, time(12), zone)))
    task = service.create_task(TaskCreate(title="Before", estimated_minutes=30))
    draft = service.propose_daily_plan(day, as_of=datetime.combine(day, time(8), zone))
    app = MobileViewApp(service, public_origin=ORIGIN)
    _, cookie = login(service, app)
    token, _ = csrf(app, cookie, f"/daily/{draft.id}")
    service.update_task(task["id"], TaskCreate(title="Changed", estimated_minutes=30))
    assert call(app, "POST", f"/daily/{draft.id}/approve", cookie=cookie,
                form={"csrf": token, "revision": str(draft.revision)})[0].startswith("409")
    assert repo.get_task(task["id"])["start_at_utc"] is None


def test_live_loopback_http_form_roundtrip_and_cli_never_binds_lan(flow_system, monkeypatch):
    from wsgiref.simple_server import make_server
    from personal_ai_os import __main__ as entry
    repo, _, _, _, _, service = flow_system
    app = MobileViewApp(service, public_origin="http://127.0.0.1:8766")
    with make_server("127.0.0.1", 0, app) as server:
        port = server.server_port
        app.origin = f"http://127.0.0.1:{port}"
        app.allowed_hosts = {f"127.0.0.1:{port}"}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            device = service.create_remote_device("HTTP phone")
            task = service.create_task(TaskCreate(title="HTTP task"))
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            body = urlencode({"token": device["token"]})
            conn.request("POST", "/session", body=body, headers={
                "Origin": app.origin, "Content-Type": "application/x-www-form-urlencoded"})
            response = conn.getresponse()
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            assert response.status == 303 and "Secure" in response.getheader("Set-Cookie")
            response.read()
            conn.request("GET", "/dashboard", headers={"Cookie": cookie})
            response = conn.getresponse()
            assert response.status == 200 and response.getheader("Cache-Control") == "no-store"
            assert response.getheader("Referrer-Policy") == "same-origin"
            dashboard = response.read().decode()
            assert "HTTP task" in dashboard
            csrf_value = re.search(r"name='csrf' value='([0-9a-f]+)'", dashboard).group(1)
            form = urlencode({"csrf": csrf_value, "version": task["version"],
                              "title": "HTTP edited", "priority": "medium",
                              "estimated_minutes": "30", "due_at_utc": "",
                              "start_at_utc": "", "end_at_utc": ""})
            conn.request("POST", f"/tasks/{task['id']}/edit", body=form, headers={
                "Cookie": cookie, "Origin": app.origin,
                "Content-Type": "application/x-www-form-urlencoded"})
            response = conn.getresponse()
            assert response.status == 303
            response.read()
            assert repo.get_task(task["id"])["title"] == "HTTP edited"
            conn.close()
        finally:
            server.shutdown()
            thread.join(timeout=3)
    captured = {}
    class FakeServer:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def serve_forever(self): captured["served"] = True
    monkeypatch.setattr("personal_ai_os.bootstrap.build_service", lambda: service)
    monkeypatch.setattr("wsgiref.simple_server.make_server", lambda host, port, app:
                        (captured.update(host=host, port=port, app=app) or FakeServer()))
    monkeypatch.setattr(sys, "argv", ["personal_ai_os", "mobile-view", "--port", "8766",
                                   "--public-origin", ORIGIN])
    monkeypatch.setenv("DATABASE_PATH", str(repo.path))
    assert entry.main() == 0
    assert captured["host"] == "127.0.0.1" and captured["port"] == 8766
    assert isinstance(captured["app"], MobileViewApp) and captured["app"].origin == ORIGIN
