"""Gmail contract tests with no account, browser, keychain, or network access."""

import base64
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from personal_ai_os import migrations
from personal_ai_os.external import ExternalKnownFailure, ExternalUnknownResult
from personal_ai_os.gmail import GmailAdapter, GmailOAuth, GoogleHTTP
from personal_ai_os.storage import Repository


ACCOUNT = "owner@example.test"


class FakeKeychain:
    def __init__(self):
        self.items = {}

    def get(self, account):
        return self.items.get(account)

    def set(self, account, value):
        self.items[account] = value


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.mode = "success"
        self.sent = 0
        self.account = ACCOUNT

    def request(self, method, url, *, token=None, form=None, body=None, write=False):
        self.calls.append((method, url, token, form, body, write))
        if url.endswith("/token"):
            if form.get("grant_type") == "authorization_code":
                return {"access_token": "access-secret", "refresh_token": "refresh-secret",
                        "scope": "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"}
            return {"access_token": "access-secret"}
        if url.endswith("/profile"):
            return {"emailAddress": self.account}
        if "/messages?" in url:
            if "pageToken=p2" in url:
                return {"messages": [{"id": "m2"}]}
            return {"messages": [{"id": "m1"}], "nextPageToken": "p2"}
        if "format=metadata" in url:
            return {"payload": {"headers": [
                {"name": "From", "value": "Tutor <tutor@example.test>"},
                {"name": "Subject", "value": "Learning plan"},
                {"name": "Date", "value": "Fri, 9 Oct 2026 09:00:00 +1100"},
            ]}}
        if "format=full" in url:
            data = base64.urlsafe_b64encode("private incoming body".encode()).decode()
            return {"payload": {"mimeType": "text/plain", "body": {"data": data},
                                "headers": [{"name": "Subject", "value": "Learning plan"}]}}
        if url.endswith("/messages/send"):
            self.sent += 1
            if self.mode == "timeout":
                raise TimeoutError("gmail_send_result_unknown")
            if self.mode == "unknown":
                raise ExternalUnknownResult("gmail_send_result_unknown")
            if self.mode == "denied":
                raise ExternalKnownFailure("gmail_http_403")
            return {"id": "sent-1", "threadId": "thread-1"}
        raise AssertionError(url)


class FakeOAuth:
    def __init__(self, transport):
        self.transport = transport

    def authorize(self, expected_account):
        return {"account": expected_account, "connected": True}

    def verified_access_token(self, expected_account):
        assert expected_account == ACCOUNT
        profile = self.transport.request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile")
        if profile["emailAddress"] != expected_account:
            raise PermissionError("gmail_account_mismatch")
        return "access-secret"


def connected(flow_system):
    repo, _, gateway, _, harness, service = flow_system
    transport = FakeTransport()
    oauth = FakeOAuth(transport)
    service.gmail_account = ACCOUNT
    service.gmail_oauth = oauth
    service.gmail_adapter = GmailAdapter(ACCOUNT, oauth)
    status = service.connect_gmail()
    return repo, gateway, harness, service, transport, status


def allow(service, status, operation):
    server = next(item for item in service.list_external_servers()
                  if item["id"] == status["server_id"])
    if server["status"] != "active":
        server = service.set_external_server_status(server["id"], server["version"], True)
    return service.set_external_operation_enabled(server["id"], operation,
                                                  server["version"], True)


def test_gmail_directory_scope_pagination_and_on_demand_body(flow_system):
    repo, _, _, service, transport, status = connected(flow_system)
    assert status["connected"] and status["account"] == ACCOUNT
    server = next(item for item in service.list_external_servers() if item["id"] == status["server_id"])
    assert server["status"] == "paused" and all(not op["enabled"] for op in server["operations"])
    with pytest.raises(PermissionError, match="external_operation_not_allowed"):
        service.list_gmail_messages()
    allow(service, status, "list_messages")
    first = service.list_gmail_messages(5)
    assert first["source"]["account_id"] == ACCOUNT
    assert first["items"] == [{"id": "m1", "from": "Tutor <tutor@example.test>",
                               "subject": "Learning plan", "date": "Fri, 9 Oct 2026 09:00:00 +1100"}]
    assert first["next_page_token"] == "p2"
    assert service.list_gmail_messages(5, "p2")["items"][0]["id"] == "m2"
    service.list_gmail_messages(5, folder="SENT")
    assert any("labelIds=SENT" in call[1] for call in transport.calls)
    assert not any("format=full" in call[1] for call in transport.calls)
    with pytest.raises(PermissionError):
        service.get_gmail_message("m1")
    allow(service, status, "get_message")
    selected = service.get_gmail_message("m1")
    assert selected["items"][0]["body"] == "private incoming body"
    assert selected["source"]["operation"] == "get_message"
    assert "private incoming body" not in json.dumps(service.list_external_audit())
    assert "private incoming body" not in repo.path.read_bytes().decode("latin1")
    with pytest.raises(ValueError, match="invalid_gmail_query"):
        service.list_gmail_messages(50)
    with pytest.raises(ValueError, match="invalid_gmail_query"):
        service.list_gmail_messages(folder="ALL")
    with pytest.raises(ValueError, match="invalid_gmail_message_id"):
        service.get_gmail_message("../secret")


@pytest.mark.parametrize("mode,status", [("success", "succeeded"), ("denied", "failed"),
                                          ("timeout", "timeout"), ("unknown", "unknown")])
def test_gmail_item_review_failure_and_no_resend(flow_system, mode, status):
    repo, _, _, service, transport, binding = connected(flow_system)
    allow(service, binding, "send_message")
    proposal = service.propose_gmail_send("test@example.test", "Hello", "private outgoing body")
    assert proposal["status"] == "pending" and transport.sent == 0
    assert proposal["payload"] == {"subject": "Hello", "body": "private outgoing body"}
    transport.mode = mode
    result = service.approve_external_write(proposal["id"], 0)
    assert result["status"] == status and transport.sent == 1
    assert service.approve_external_write(proposal["id"], 0)["status"] == status
    assert transport.sent == 1
    if status in {"timeout", "unknown"}:
        assert service.propose_gmail_send("test@example.test", "Hello", "private outgoing body")["id"] == proposal["id"]
    if status == "succeeded":
        assert result["result"]["message_id"] == "sent-1"
    assert "private outgoing body" not in json.dumps(service.list_external_audit())
    assert "access-secret" not in repo.path.read_bytes().decode("latin1")


def test_account_mismatch_and_mail_validation(flow_system):
    _, _, _, service, transport, binding = connected(flow_system)
    allow(service, binding, "list_messages")
    transport.account = "other@example.test"
    with pytest.raises(PermissionError, match="gmail_account_mismatch"):
        service.list_gmail_messages()
    assert not any("/messages?" in call[1] for call in transport.calls)
    allow(service, binding, "send_message")
    with pytest.raises(ValueError, match="invalid_mail_recipient"):
        service.propose_gmail_send("x@example.test,other@example.test", "Hello", "Body")
    with pytest.raises(ValueError, match="invalid_mail_payload"):
        service.propose_gmail_send("x@example.test", "Bad\nSubject", "Body")


def test_oauth_account_check_before_keychain_write(tmp_path):
    client = tmp_path / "desktop.json"
    client.write_text(json.dumps({"installed": {
        "client_id": "client", "client_secret": "desktop-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}))
    client.chmod(0o600)
    keychain, transport = FakeKeychain(), FakeTransport()
    oauth = GmailOAuth(client, keychain, transport)
    transport.account = "other@example.test"
    with pytest.raises(PermissionError, match="gmail_account_mismatch"):
        oauth.finish_code(ACCOUNT, "code", "http://127.0.0.1:10000/", "verifier")
    assert keychain.items == {}
    transport.account = ACCOUNT
    assert oauth.finish_code(ACCOUNT, "code", "http://127.0.0.1:10000/", "verifier")["connected"]
    assert keychain.items == {ACCOUNT: "refresh-secret"}
    assert oauth.verified_access_token(ACCOUNT) == "access-secret"
    original_request = transport.request
    def missing_scope(method, url, **kwargs):
        result = original_request(method, url, **kwargs)
        if url.endswith("/token") and kwargs.get("form", {}).get("grant_type") == "authorization_code":
            result["scope"] = "https://www.googleapis.com/auth/gmail.send"
        return result
    transport.request = missing_scope
    with pytest.raises(PermissionError, match="gmail_required_scopes_missing"):
        oauth.finish_code(ACCOUNT, "code", "http://127.0.0.1:10000/", "verifier")
    assert keychain.items == {ACCOUNT: "refresh-secret"}
    client.chmod(0o644)
    with pytest.raises(PermissionError, match="gmail_oauth_client_file_permissions"):
        oauth.verified_access_token(ACCOUNT)


def test_desktop_oauth_loopback_state_and_pkce(tmp_path):
    client = tmp_path / "desktop.json"
    client.write_text(json.dumps({"installed": {
        "client_id": "client", "client_secret": "desktop-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}))
    client.chmod(0o600)
    keychain, transport = FakeKeychain(), FakeTransport()
    oauth = GmailOAuth(client, keychain, transport)
    requests = []
    def browser_open(url):
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert set(params["scope"][0].split()) == {
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        }
        assert params["code_challenge_method"] == ["S256"]
        requests.append(params)
        callback = params["redirect_uri"][0] + "?" + urllib.parse.urlencode({
            "code": "one-time-code", "state": params["state"][0]
        })
        threading.Thread(target=lambda: urllib.request.urlopen(callback, timeout=3).close(),
                         daemon=True).start()
        return True
    assert oauth.authorize(ACCOUNT, browser_open=browser_open)["connected"]
    assert requests and keychain.get(ACCOUNT) == "refresh-secret"
    exchange = next(call for call in transport.calls if call[3] and call[3].get("grant_type") == "authorization_code")
    assert exchange[3]["code_verifier"] and exchange[3]["redirect_uri"].startswith("http://127.0.0.1:")
    before = dict(keychain.items)
    def wrong_state(url):
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        callback = params["redirect_uri"][0] + "?code=wrong&state=wrong"
        def visit():
            try:
                urllib.request.urlopen(callback, timeout=3).close()
            except urllib.error.HTTPError:
                pass
        threading.Thread(target=visit, daemon=True).start()
        return True
    with pytest.raises(RuntimeError, match="gmail_oauth_not_completed"):
        oauth.authorize(ACCOUNT, browser_open=wrong_state)
    assert keychain.items == before


def test_http_send_error_classification(monkeypatch):
    def error(code):
        def run(*_args, **_kwargs):
            raise urllib.error.HTTPError("https://gmail.googleapis.com/", code, "error", {}, io.BytesIO())
        return run
    http = GoogleHTTP()
    monkeypatch.setattr("urllib.request.urlopen", error(403))
    with pytest.raises(ExternalKnownFailure, match="gmail_http_403"):
        http.request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                     token="secret", body={"raw": "abc"}, write=True)
    monkeypatch.setattr("urllib.request.urlopen", error(503))
    with pytest.raises(ExternalUnknownResult, match="gmail_send_result_unknown"):
        http.request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                     token="secret", body={"raw": "abc"}, write=True)


def test_v10_to_v11_backup_and_rollback(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    repo = Repository(path)
    repo.initialize()
    from personal_ai_os.external import ExternalGateway
    server = ExternalGateway(repo).register_server("Old mail", "mail")
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM mcp_operations WHERE server_id=? AND operation='get_message'", (server["id"],))
        conn.execute("PRAGMA user_version=10")
    original = migrations.migrate_v11
    def fail(conn):
        original(conn)
        raise RuntimeError("injected_v11_failure")
    monkeypatch.setattr(migrations, "migrate_v11", fail)
    with pytest.raises(RuntimeError, match="injected_v11_failure"):
        repo.initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM mcp_operations WHERE server_id=? AND operation='get_message'",
                            (server["id"],)).fetchone()[0] == 0
    assert list(tmp_path.glob("old.backup-v10-*.sqlite3"))
    monkeypatch.setattr(migrations, "migrate_v11", original)
    repo.initialize()
    with repo.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION
        assert conn.execute("SELECT enabled FROM mcp_operations WHERE server_id=? AND operation='get_message'",
                            (server["id"],)).fetchone()[0] == 0
