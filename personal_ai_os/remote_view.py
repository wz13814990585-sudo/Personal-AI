"""Minimal protected read-only browser view; launcher binds loopback only."""

from __future__ import annotations

import html
import json
from http.cookies import SimpleCookie
from typing import Callable, Iterable
from urllib.parse import parse_qs

from .display_zh import zh
from .services import RemoteReadService


_HEADERS = [
    ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"),
]


def _page(content: str) -> bytes:
    return ("<!doctype html><html lang='zh'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Personal AI OS 只读视图</title>"
            "<style>body{font:16px system-ui;max-width:760px;margin:2rem auto;padding:0 1rem}"
            "li{margin:.55rem 0}input,button{font:inherit;padding:.5rem}</style>"
            f"<body>{content}</body></html>").encode("utf-8")


def _login_page(error: bool = False) -> bytes:
    message = "<p>凭据无效或已失效。</p>" if error else ""
    return _page("<h1>Personal AI OS 只读视图</h1>"
                 "<p>输入在本机设置页签发的设备凭据。此视图只能查看任务与提醒。</p>"
                 + message + "<form method='post' action='/session'>"
                 "<label>设备凭据 <input type='password' name='token' autocomplete='off' required></label>"
                 "<button type='submit'>查看</button></form>")


def _dashboard(snapshot: dict) -> bytes:
    tasks = "".join(
        "<li>" + html.escape(item["title"]) + " · " + html.escape(zh(item["status"]))
        + (" · " + html.escape(item["start_at_utc"]) if item["start_at_utc"] else "") + "</li>"
        for item in snapshot["tasks"]
    )
    notices = "".join(
        "<li>" + html.escape(item["title"]) + " · " + html.escape(item["body"])
        + " · " + html.escape(zh(item["status"])) + "</li>"
        for item in snapshot["notifications"]
    )
    return _page("<h1>任务与提醒（只读）</h1><h2>未完成任务</h2><ul>" + tasks +
                 "</ul><h2>提醒</h2><ul>" + notices + "</ul>"
                 "<form method='post' action='/logout'><button type='submit'>退出</button></form>")


class RemoteViewApp:
    def __init__(self, service: RemoteReadService):
        self.service = service

    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
        cookies = SimpleCookie()
        try:
            cookies.load(environ.get("HTTP_COOKIE", ""))
            session = cookies["personal_ai_session"].value if "personal_ai_session" in cookies else ""
        except Exception:
            session = ""

        def respond(status: str, body: bytes, content_type: str = "text/html; charset=utf-8",
                    extra: list[tuple[str, str]] | None = None) -> list[bytes]:
            headers = _HEADERS + [("Content-Type", content_type), ("Content-Length", str(len(body)))]
            start_response(status, headers + (extra or []))
            return [body]

        if method == "GET" and path == "/":
            return respond("200 OK", _login_page())
        if method == "POST" and path == "/session":
            try:
                length = int(environ.get("CONTENT_LENGTH", "0"))
                if length < 1 or length > 1024:
                    raise ValueError
                raw = environ["wsgi.input"].read(length).decode("utf-8")
                token = parse_qs(raw, keep_blank_values=True).get("token", [""])[0]
                new_session = self.service.open_remote_session(token)
            except (ValueError, UnicodeError, KeyError, PermissionError):
                return respond("401 Unauthorized", _login_page(True))
            return respond("303 See Other", b"", extra=[
                ("Location", "/dashboard"),
                ("Set-Cookie", "personal_ai_session=" + new_session +
                 "; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"),
            ])
        if method == "POST" and path == "/logout":
            self.service.close_remote_session(session)
            return respond("303 See Other", b"", extra=[
                ("Location", "/"),
                ("Set-Cookie", "personal_ai_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"),
            ])
        if method == "GET" and path in {"/dashboard", "/api/overview"}:
            try:
                snapshot = self.service.remote_snapshot(session)
            except PermissionError:
                return respond("401 Unauthorized", _login_page(True))
            if path == "/api/overview":
                return respond("200 OK", json.dumps(snapshot, ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
            return respond("200 OK", _dashboard(snapshot))
        return respond("404 Not Found", _page("<h1>未找到页面</h1>"))
