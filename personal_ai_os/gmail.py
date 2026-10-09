"""Local desktop OAuth and narrowly scoped Gmail read/send adapter."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from email.message import EmailMessage
from email.policy import SMTP
from html import unescape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .external import ExternalKnownFailure, ExternalUnknownResult


READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = (READ_SCOPE, SEND_SCOPE)
API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class MacKeychainStore:
    """Refresh tokens pass to Security.framework over stdin, never in arguments or SQLite."""

    def __init__(self, data_directory: Path):
        self.directory = Path(data_directory)
        self.executable = self.directory / "GmailKeychain"
        self.source = Path(__file__).with_name("gmail_keychain.swift")

    def _build(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("gmail_keychain_requires_macos")
        if self.executable.exists() and self.executable.stat().st_mtime >= self.source.stat().st_mtime:
            return
        compiler = shutil.which("swiftc")
        if compiler is None:
            raise RuntimeError("swift_compiler_unavailable")
        self.directory.mkdir(parents=True, exist_ok=True)
        cache = self.directory / "ModuleCache"
        cache.mkdir(exist_ok=True)
        temp = self.executable.with_suffix(".building")
        result = subprocess.run(
            [compiler, "-parse-as-library", "-module-cache-path", str(cache),
             "-o", str(temp), str(self.source)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode:
            raise RuntimeError("gmail_keychain_build_failed: " + result.stderr[-1200:])
        os.replace(temp, self.executable)

    def _call(self, operation: str, account: str, value: str | None = None) -> dict[str, Any]:
        self._build()
        request = {"operation": operation, "account": account}
        if value is not None:
            request["value"] = value
        result = subprocess.run(
            [str(self.executable)], input=json.dumps(request).encode(),
            capture_output=True, timeout=15,
        )
        try:
            data = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("gmail_keychain_unavailable") from exc
        if result.returncode or data.get("ok") is not True:
            raise RuntimeError("gmail_keychain_unavailable")
        return data

    def get(self, account: str) -> str | None:
        data = self._call("get", account)
        return data.get("value") if data.get("present") else None

    def set(self, account: str, value: str) -> None:
        self._call("set", account, value)

    def delete(self, account: str) -> None:
        self._call("delete", account)


class GoogleHTTP:
    def request(self, method: str, url: str, *, token: str | None = None,
                form: dict[str, str] | None = None, body: dict[str, Any] | None = None,
                write: bool = False) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(url)
        if not (parsed.scheme == "https" and
                ((parsed.hostname == "gmail.googleapis.com" and parsed.path.startswith("/gmail/v1/")) or
                 url == "https://oauth2.googleapis.com/token")):
            raise ValueError("google_endpoint_not_allowed")
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        data = None
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = urllib.parse.urlencode(form).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            if write and exc.code >= 500:
                raise ExternalUnknownResult("gmail_send_result_unknown") from None
            raise ExternalKnownFailure("gmail_http_" + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if write:
                raise TimeoutError("gmail_send_result_unknown") from None
            raise RuntimeError("gmail_connection_failed") from None
        if len(raw) > 2_000_000:
            raise RuntimeError("gmail_response_too_large")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            if write:
                raise ExternalUnknownResult("gmail_send_invalid_response") from None
            raise RuntimeError("gmail_invalid_response") from None
        if not isinstance(result, dict):
            if write:
                raise ExternalUnknownResult("gmail_send_invalid_response")
            raise RuntimeError("gmail_invalid_response")
        return result


class GmailOAuth:
    def __init__(self, client_path: Path, keychain: MacKeychainStore,
                 transport: GoogleHTTP | None = None):
        self.client_path = Path(client_path)
        self.keychain = keychain
        self.transport = transport or GoogleHTTP()

    def _client(self) -> dict[str, str]:
        if not self.client_path.is_file():
            raise RuntimeError("gmail_oauth_client_file_missing")
        if self.client_path.stat().st_mode & 0o077:
            raise PermissionError("gmail_oauth_client_file_permissions")
        try:
            config = json.loads(self.client_path.read_text(encoding="utf-8"))["installed"]
            if config["token_uri"] != "https://oauth2.googleapis.com/token":
                raise ValueError
            if config["auth_uri"] not in {
                "https://accounts.google.com/o/oauth2/auth",
                "https://accounts.google.com/o/oauth2/v2/auth",
            }:
                raise ValueError
            return {key: config[key] for key in ("client_id", "client_secret", "auth_uri", "token_uri")}
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("invalid_gmail_desktop_client") from exc

    def finish_code(self, expected_account: str, code: str, redirect_uri: str,
                    verifier: str) -> dict[str, Any]:
        client = self._client()
        tokens = self.transport.request("POST", client["token_uri"], form={
            "code": code, "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect_uri, "grant_type": "authorization_code",
            "code_verifier": verifier,
        })
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        if not isinstance(access, str) or not isinstance(refresh, str):
            raise RuntimeError("gmail_refresh_token_missing")
        granted = tokens.get("scope")
        if not isinstance(granted, str) or not set(SCOPES) <= set(granted.split()):
            raise PermissionError("gmail_required_scopes_missing")
        profile = self.transport.request("GET", API_ROOT + "/profile", token=access)
        actual = str(profile.get("emailAddress", "")).casefold()
        if actual != expected_account.casefold():
            raise PermissionError("gmail_account_mismatch")
        self.keychain.set(expected_account, refresh)
        return {"account": expected_account, "connected": True}

    def authorize(self, expected_account: str, browser_open=webbrowser.open) -> dict[str, Any]:
        client = self._client()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        received: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                values = urllib.parse.parse_qs(parts.query)
                if parts.path == "/" and values.get("state") == [state]:
                    received["code"] = values.get("code", [""])[0]
                self.send_response(200 if received.get("code") else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"You may return to Personal AI OS.")

            def log_message(self, *_args):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = 180
            redirect = f"http://127.0.0.1:{server.server_port}/"
            query = urllib.parse.urlencode({
                "client_id": client["client_id"], "redirect_uri": redirect,
                "response_type": "code", "scope": " ".join(SCOPES),
                "access_type": "offline", "prompt": "consent",
                "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256",
            })
            if not browser_open(client["auth_uri"] + "?" + query):
                raise RuntimeError("gmail_browser_open_failed")
            server.handle_request()
        if not received.get("code"):
            raise RuntimeError("gmail_oauth_not_completed")
        return self.finish_code(expected_account, received["code"], redirect, verifier)

    def verified_access_token(self, expected_account: str) -> str:
        refresh = self.keychain.get(expected_account)
        if not refresh:
            raise RuntimeError("gmail_not_authorized")
        client = self._client()
        tokens = self.transport.request("POST", client["token_uri"], form={
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "refresh_token": refresh, "grant_type": "refresh_token",
        })
        access = tokens.get("access_token")
        if not isinstance(access, str):
            raise RuntimeError("gmail_token_refresh_failed")
        profile = self.transport.request("GET", API_ROOT + "/profile", token=access)
        if str(profile.get("emailAddress", "")).casefold() != expected_account.casefold():
            raise PermissionError("gmail_account_mismatch")
        return access


def _header(payload: dict[str, Any], name: str) -> str:
    for item in payload.get("headers", []):
        if str(item.get("name", "")).lower() == name.lower():
            return str(item.get("value", ""))[:1000]
    return ""


def _text_body(part: dict[str, Any]) -> tuple[str, str]:
    if part.get("filename"):
        return "", ""
    body = part.get("body") or {}
    encoded = body.get("data")
    mime = part.get("mimeType", "")
    if isinstance(encoded, str) and mime in {"text/plain", "text/html"}:
        try:
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode(
                "utf-8", errors="replace"
            )[:200_000]
        except ValueError:
            decoded = ""
        if mime == "text/html":
            decoded = unescape(re.sub(r"<[^>]*>", " ", decoded))
        return (decoded, "") if mime == "text/plain" else ("", decoded)
    plain, html = "", ""
    for child in part.get("parts") or []:
        p, h = _text_body(child)
        plain += p
        html += h
    return plain[:200_000], html[:200_000]


class GmailAdapter:
    def __init__(self, expected_account: str, oauth: GmailOAuth):
        self.expected_account = expected_account
        self.oauth = oauth

    def _token(self, account_id: str) -> str:
        if account_id.casefold() != self.expected_account.casefold():
            raise PermissionError("gmail_account_mismatch")
        return self.oauth.verified_access_token(self.expected_account)

    def list_messages(self, account_id: str, query: dict[str, Any]) -> dict[str, Any]:
        if set(query) - {"page_size", "page_token", "folder"}:
            raise ValueError("invalid_gmail_query")
        page_size = query.get("page_size", 10)
        page_token = query.get("page_token", "")
        folder = query.get("folder", "INBOX")
        if (type(page_size) is not int or not 1 <= page_size <= 20 or
                not isinstance(page_token, str) or len(page_token) > 1000 or
                folder not in {"INBOX", "SENT"}):
            raise ValueError("invalid_gmail_query")
        token = self._token(account_id)
        params = {"maxResults": str(page_size), "labelIds": folder}
        if page_token:
            params["pageToken"] = page_token
        page = self.oauth.transport.request(
            "GET", API_ROOT + "/messages?" + urllib.parse.urlencode(params), token=token
        )
        items = []
        for entry in page.get("messages", []):
            message_id = entry.get("id", "")
            if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
                raise RuntimeError("gmail_invalid_message_id")
            metadata = self.oauth.transport.request(
                "GET", API_ROOT + "/messages/" + message_id + "?" +
                urllib.parse.urlencode({"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}, doseq=True),
                token=token,
            )
            payload = metadata.get("payload") or {}
            items.append({"id": message_id, "from": _header(payload, "From"),
                          "subject": _header(payload, "Subject"),
                          "date": _header(payload, "Date")})
        next_token = page.get("nextPageToken", "")
        if not isinstance(next_token, str) or len(next_token) > 1000:
            raise RuntimeError("gmail_invalid_page_token")
        return {"items": items, "next_page_token": next_token}

    def get_message(self, account_id: str, message_id: str) -> list[dict[str, Any]]:
        if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
            raise ValueError("invalid_gmail_message_id")
        token = self._token(account_id)
        item = self.oauth.transport.request(
            "GET", API_ROOT + "/messages/" + message_id + "?format=full", token=token
        )
        payload = item.get("payload") or {}
        plain, html = _text_body(payload)
        return [{"id": message_id, "from": _header(payload, "From"),
                 "subject": _header(payload, "Subject"), "date": _header(payload, "Date"),
                 "body": plain or html, "body_format": "plain" if plain else "html_as_text"}]

    def send_message(self, account_id: str, recipient: str, message: dict[str, Any],
                     idempotency_key: str) -> dict[str, Any]:
        token = self._token(account_id)
        if not re.fullmatch(r"[^@\s<>;,]+@[^@\s<>;,]+\.[^@\s<>;,]+", recipient):
            raise ExternalKnownFailure("invalid_mail_recipient")
        subject, body = message.get("subject"), message.get("body")
        if (not isinstance(subject, str) or not subject.strip() or "\r" in subject or "\n" in subject
                or not isinstance(body, str) or not body.strip()):
            raise ExternalKnownFailure("invalid_mail_payload")
        mail = EmailMessage()
        mail["From"] = self.expected_account
        mail["To"] = recipient
        mail["Subject"] = subject
        mail["Message-ID"] = f"<{idempotency_key}@personal-ai-os.local>"
        mail.set_content(body)
        raw = base64.urlsafe_b64encode(mail.as_bytes(policy=SMTP)).decode().rstrip("=")
        result = self.oauth.transport.request(
            "POST", API_ROOT + "/messages/send", token=token,
            body={"raw": raw}, write=True,
        )
        if not isinstance(result.get("id"), str):
            raise ExternalUnknownResult("gmail_send_invalid_response")
        return {"message_id": result["id"], "thread_id": result.get("threadId")}
