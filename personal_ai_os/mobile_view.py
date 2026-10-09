"""Loopback-only mobile HTML view behind an explicitly configured private HTTPS proxy.

All business reads and writes go through PersonalAIService. The proxy supplies transport
protection; this app supplies device sessions, CSRF, origin checks and bounded forms.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import io
import json
import re
import time
from collections import defaultdict, deque
from datetime import datetime
from http.cookies import SimpleCookie
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo
from pydantic import ValidationError

from .contracts import DailyPlanAction, TaskCreate, TaskDraft
from .display_zh import zh, zh_explanation, zh_issue
from .services import PersonalAIService

_ID_PATH = re.compile(r"^/(tasks|plans|daily|memory|external)/([0-9a-f]{32})(?:/(edit|approve|reject))?$")
_HEADERS = [
    ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"), ("Referrer-Policy", "same-origin"),
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
     "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
]


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _field(label: str, name: str, value: Any = "", *, kind: str = "text") -> str:
    return f"<label>{_e(label)}<input name='{name}' type='{kind}' value='{_e(value)}'></label>"


def _priority_field(value: str) -> str:
    options = "".join(
        f"<option value='{code}'{' selected' if code == value else ''}>{zh(code)}</option>"
        for code in ("low", "medium", "high")
    )
    return "<label>优先级<select name='priority'>" + options + "</select></label>"


def _error_detail(exc: Exception) -> str:
    code = str(exc)[:200]
    message = zh(code)
    if message == code:
        message = "操作未完成，请核对输入或刷新后重试。"
    return ("<p>" + _e(message) + "</p><details><summary>技术错误代码</summary><code>" +
            _e(code) + "</code></details>")


def _hidden(name: str, value: Any) -> str:
    return f"<input type='hidden' name='{name}' value='{_e(value)}'>"


def _form(action: str, csrf: str, label: str, body: str = "") -> str:
    return (f"<form method='post' action='{_e(action)}'>" + _hidden("csrf", csrf) +
            body + f"<button type='submit'>{_e(label)}</button></form>")


def _link(path: str, label: Any) -> str:
    return f"<a href='{_e(path)}'>{_e(label)}</a>"


def _parse_time(value: str) -> datetime | None:
    if not value.strip():
        return None
    result = datetime.fromisoformat(value.strip())
    if result.tzinfo is None:
        raise ValueError("time_requires_timezone")
    return result


class MobileViewApp:
    def __init__(self, service: PersonalAIService, *, public_origin: str = "http://127.0.0.1:8766",
                 local_port: int = 8766):
        parsed = urlsplit(public_origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or
            parsed.query or parsed.fragment or parsed.username or parsed.password or
            (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"})):
            raise ValueError("invalid_mobile_public_origin")
        self.service = service
        self.origin = public_origin.rstrip("/")
        self.allowed_hosts = {parsed.netloc, f"127.0.0.1:{local_port}", f"localhost:{local_port}"}
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def _csrf(session: str) -> str:
        return hmac.new(session.encode(), b"personal-ai-mobile-csrf-v1", hashlib.sha256).hexdigest()

    def _rate(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        recent = self._requests[key]
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= limit:
            return False
        recent.append(now)
        return True

    def _origin_ok(self, environ: dict) -> bool:
        origin = environ.get("HTTP_ORIGIN", "")
        if origin:
            return origin == self.origin
        referer = environ.get("HTTP_REFERER", "")
        return bool(referer and (referer == self.origin or referer.startswith(self.origin + "/")))

    @staticmethod
    def _body(environ: dict) -> dict[str, str]:
        try:
            length = int(environ.get("CONTENT_LENGTH", ""))
            if not 0 < length <= 8192:
                raise ValueError
            raw = environ["wsgi.input"].read(length).decode("utf-8", errors="strict")
            values = parse_qs(raw, keep_blank_values=True, max_num_fields=30)
            if any(len(items) != 1 for items in values.values()):
                raise ValueError
            return {key: value[0] for key, value in values.items()}
        except (KeyError, TypeError, UnicodeError, ValueError):
            raise ValueError("invalid_mobile_form") from None

    def _page(self, title: str, content: str, csrf: str = "") -> bytes:
        nav = _link("/dashboard", "首页") if csrf else ""
        logout = _form("/logout", csrf, "退出") if csrf else ""
        return ("<!doctype html><html lang='zh'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{_e(title)} · Personal AI OS</title>"
                "<style>body{font:16px system-ui;max-width:720px;margin:auto;padding:1rem}"
                "section,article{border:1px solid #aaa;border-radius:.5rem;padding:1rem;margin:1rem 0}"
                "label{display:block;margin:.6rem 0}input,textarea,button{font:inherit;max-width:100%}"
                "input:not([type=hidden]),textarea{display:block;width:100%;box-sizing:border-box;padding:.5rem}"
                "button{padding:.6rem;margin:.4rem 0}a{overflow-wrap:anywhere}</style>"
                f"<body><nav>{nav}</nav><h1>{_e(title)}</h1>{content}{logout}</body></html>").encode()

    def _login(self, bad: bool = False) -> bytes:
        return self._page("手机访问", ("<p>设备凭据无效或已过期。</p>" if bad else "") +
                          "<p>此入口需要 Mac 设置页签发的设备凭据。所有编辑和审批都需确认。</p>"
                          "<form method='post' action='/session'><label>设备凭据"
                          "<input type='password' name='token' autocomplete='off' required></label>"
                          "<button type='submit'>登录</button></form>")

    def _dashboard(self, csrf: str) -> bytes:
        tasks = self.service.list_tasks()
        plans = [item for item in self.service.list_runs() if item["kind"] == "planning" and item["status"] == "waiting_approval"]
        daily = [item for item in self.service.list_daily_plans() if item.status == "draft"]
        memory = self.service.list_memory_proposals("pending")
        external = [item for item in self.service.list_external_write_proposals() if item["status"] == "pending"]
        sections = [
            ("任务", [_link("/tasks/" + item["id"], item["title"] + " · " + zh(item["status"]))
                    for item in tasks[:100]]),
            ("待审规划", [_link("/plans/" + item["id"], item["request_text"][:90])
                      for item in plans[:20]]),
            ("待审每日计划", [_link("/daily/" + item.id, item.local_date.isoformat())
                        for item in daily[:20]]),
            ("待审记忆", [_link("/memory/" + item["id"], zh(item["kind"]) + " · " + item["source_excerpt"][:80])
                      for item in memory[:20]]),
            ("待审外部写入", [_link("/external/" + item["id"], zh(item["operation"]) + " → " + item["target"])
                        for item in external[:20]]),
        ]
        content = "".join("<section><h2>" + _e(title) + "</h2><ul>" +
                          "".join("<li>" + row + "</li>" for row in rows) +
                          "</ul></section>" for title, rows in sections)
        return self._page("今日与审批", content, csrf)

    def _task(self, task_id: str, csrf: str) -> bytes:
        item = next((row for row in self.service.list_tasks() if row["id"] == task_id), None)
        if item is None:
            raise KeyError(task_id)
        body = _hidden("version", item["version"])
        for label, key in (("标题", "title"),
                           ("预计分钟", "estimated_minutes"), ("期限 ISO 8601（带时区）", "due_at_utc"),
                           ("开始 ISO 8601（带时区）", "start_at_utc"),
                           ("结束 ISO 8601（带时区）", "end_at_utc")):
            body += _field(label, key, item.get(key) or "")
        body += _priority_field(item["priority"])
        content = (f"<p>当前状态：{_e(zh(item['status']))}；版本：{item['version']}</p>" +
                   _form(f"/tasks/{task_id}/edit", csrf, "保存任务修改", body))
        if item.get("resource_url"):
            content += f"<p>学习资料：<a href='{_e(item['resource_url'])}' rel='noopener noreferrer'>{_e(item['resource_source'])}</a></p>"
        return self._page("编辑任务", content, csrf)

    def _plan(self, run_id: str, csrf: str) -> bytes:
        run = self.service.get_run(run_id)
        draft = self.service.get_plan(run_id)
        content = f"<p>状态：{_e(zh(run['status']))}；运行编号：{_e(run_id)}；版本：{draft.revision}</p>"
        content += "".join("<p>" + _e(text) + "</p>" for text in draft.explanations)
        content += "".join("<p>冲突：" + _e(zh_issue(text)) + "</p>" for text in draft.conflicts)
        for task in draft.tasks:
            body = _hidden("revision", draft.revision) + _hidden("item_id", task.draft_item_id)
            for label, key, value in (("标题", "title", task.title),
                                      ("预计分钟", "estimated_minutes", task.estimated_minutes),
                                      ("开始 ISO 8601（带时区）", "start_at", task.start_at or ""),
                                      ("结束 ISO 8601（带时区）", "end_at", task.end_at or "")):
                body += _field(label, key, value)
            body += _priority_field(task.priority)
            resource = (f"<p><a href='{_e(task.resource_url)}' rel='noopener noreferrer'>"
                        f"{_e(task.resource_channel)} · {_e(task.resource_duration_seconds)} 秒 · {_e(task.resource_source)}</a></p>"
                        if task.resource_url else "")
            content += "<article><h2>" + _e(task.draft_item_id) + "</h2>" + resource
            if run["status"] == "waiting_approval":
                content += _form(f"/plans/{run_id}/edit", csrf, "保存这项草案修改", body)
            content += "</article>"
        if run["status"] == "waiting_approval":
            content += _form(f"/plans/{run_id}/approve", csrf, "确认全部任务并加入待办",
                             _hidden("revision", draft.revision))
        return self._page("规划草案", content, csrf)

    def _daily(self, plan_id: str, csrf: str) -> bytes:
        draft = self.service.get_daily_plan(plan_id)
        content = f"<p>日期：{draft.local_date.isoformat()}；状态：{zh(draft.status)}；版本：{draft.revision}</p>"
        for action in draft.actions:
            content += ("<article><h2>" + _e(action.title) + "</h2><p>" + _e(zh(action.kind)) +
                        " · 原时间 " + _e(action.old_start_at) + " → " + _e(action.old_end_at) +
                        " · 新时间 " + _e(action.new_start_at) + " → " + _e(action.new_end_at) +
                        "</p><p>依据：" + _e(zh_issue(zh_explanation(action.reason))) + "</p>")
            if draft.status == "draft":
                fields = (_hidden("revision", draft.revision) + _hidden("action_id", action.action_id) +
                          _field("新开始 ISO 8601（带时区）", "new_start_at", action.new_start_at or "") +
                          _field("新结束 ISO 8601（带时区）", "new_end_at", action.new_end_at or "") +
                          _field("安排依据", "reason", action.reason))
                content += _form(f"/daily/{plan_id}/edit", csrf, "保存这项每日草案修改", fields)
            content += "</article>"
        content += "".join("<p>冲突：" + _e(zh_issue(item)) + "</p>" for item in draft.conflicts)
        if draft.status == "draft":
            content += _form(f"/daily/{plan_id}/approve", csrf, "确认每日计划",
                             _hidden("revision", draft.revision))
        return self._page("每日计划草案", content, csrf)

    def _memory(self, proposal_id: str, csrf: str) -> bytes:
        item = next((row for row in self.service.list_memory_proposals() if row["id"] == proposal_id), None)
        if item is None:
            raise KeyError(proposal_id)
        content = ("<p>状态：" + _e(zh(item["status"])) + "；类型：" + _e(zh(item["kind"])) +
                   "</p><p>反馈摘录：" + _e(item["source_excerpt"]) + "</p><p>说明：" +
                   _e(item["explanation"]) + "</p>")
        if item["status"] == "pending":
            content += _form(f"/memory/{proposal_id}/edit", csrf, "保存候选记忆修改",
                             "<label>候选值 JSON<textarea name='value'>" +
                             _e(json.dumps(json.loads(item["value_json"]), ensure_ascii=False)) +
                             "</textarea></label>")
            content += _form(f"/memory/{proposal_id}/approve", csrf, "批准这条记忆")
            content += _form(f"/memory/{proposal_id}/reject", csrf, "拒绝这条记忆")
        return self._page("候选记忆", content, csrf)

    def _external(self, proposal_id: str, csrf: str) -> bytes:
        item = next((row for row in self.service.list_external_write_proposals() if row["id"] == proposal_id), None)
        if item is None:
            raise KeyError(proposal_id)
        content = ("<p>状态：" + _e(zh(item["status"])) + "；版本：" + str(item["revision"]) +
                   "</p><p>服务器：" + _e(item["server_id"]) + "；操作：" +
                   _e(zh(item["operation"])) + "；账号：" + _e(item["account_id"]) +
                   "；目标：" + _e(item["target"]) + "</p><pre>" +
                   _e(json.dumps(item["payload"], ensure_ascii=False, indent=2)) + "</pre>")
        if item["account_id"] == "icloud" and item["operation"] in {"create_event", "update_event"}:
            selected = self.service.get_icloud_calendar_selection()
            if selected:
                content += "<p>当前 iCloud 日历：" + _e(selected["title"]) + "</p>"
            try:
                zone = ZoneInfo(self.service.timezone)
                start = datetime.fromisoformat(item["payload"]["start_at_utc"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(item["payload"]["end_at_utc"].replace("Z", "+00:00"))
                reminder = item["payload"].get("reminder_minutes")
                content += ("<p>当地时间：" + _e(start.astimezone(zone).isoformat()) + " → " +
                            _e(end.astimezone(zone).isoformat()) + "；时区：" + _e(self.service.timezone) +
                            "；提醒：" + _e("无" if reminder is None else f"提前 {reminder} 分钟") + "</p>")
            except (KeyError, TypeError, ValueError):
                pass
        if item.get("error_code"):
            content += "<p>结果：" + _e(zh(item["error_code"])) + "</p>"
        if item["status"] == "pending":
            content += _form(f"/external/{proposal_id}/approve", csrf, "确认这一项外部写入",
                             _hidden("revision", item["revision"]))
            content += _form(f"/external/{proposal_id}/reject", csrf, "拒绝这一项外部写入",
                             _hidden("revision", item["revision"]))
        return self._page("外部写入预览", content, csrf)

    def _mutate(self, group: str, item_id: str, action: str,
                values: dict[str, str]) -> str:
        if group == "tasks" and action == "edit":
            item = next((row for row in self.service.list_tasks() if row["id"] == item_id), None)
            if item is None:
                raise KeyError(item_id)
            version = int(values["version"])
            self.service.update_task(item_id, TaskCreate(
                title=values["title"], goal_id=item["goal_id"], priority=values["priority"],
                estimated_minutes=int(values["estimated_minutes"]),
                due_at=_parse_time(values.get("due_at_utc", "")),
                start_at=_parse_time(values.get("start_at_utc", "")),
                end_at=_parse_time(values.get("end_at_utc", "")),
            ), expected_version=version)
        elif group == "plans" and action == "edit":
            draft = self.service.get_plan(item_id)
            source = next((task for task in draft.tasks if task.draft_item_id == values["item_id"]), None)
            if source is None:
                raise ValueError("unknown_draft_item")
            changed = TaskDraft.model_validate({**source.model_dump(),
                "title": values["title"], "priority": values["priority"],
                "estimated_minutes": int(values["estimated_minutes"]),
                "start_at": _parse_time(values.get("start_at", "")),
                "end_at": _parse_time(values.get("end_at", ""))})
            self.service.edit_plan(item_id, int(values["revision"]), [
                changed if task.draft_item_id == source.draft_item_id else task for task in draft.tasks
            ])
        elif group == "plans" and action == "approve":
            self.service.approve_plan(item_id, int(values["revision"]))
        elif group == "daily" and action == "edit":
            draft = self.service.get_daily_plan(item_id)
            source = next((item for item in draft.actions if item.action_id == values["action_id"]), None)
            if source is None:
                raise ValueError("unknown_daily_action")
            changed = DailyPlanAction.model_validate({**source.model_dump(),
                "new_start_at": _parse_time(values.get("new_start_at", "")),
                "new_end_at": _parse_time(values.get("new_end_at", "")),
                "reason": values["reason"]})
            self.service.edit_daily_plan(item_id, int(values["revision"]), [
                changed if item.action_id == source.action_id else item for item in draft.actions
            ])
        elif group == "daily" and action == "approve":
            self.service.approve_daily_plan(item_id, int(values["revision"]))
        elif group == "memory" and action == "edit":
            value = json.loads(values["value"])
            if not isinstance(value, dict):
                raise ValueError("invalid_memory_value")
            self.service.edit_memory_proposal(item_id, value)
        elif group == "memory" and action in {"approve", "reject"}:
            self.service.resolve_memory(item_id, action == "approve")
        elif group == "external" and action in {"approve", "reject"}:
            method = self.service.approve_external_write if action == "approve" else self.service.reject_external_write
            method(item_id, int(values["revision"]))
        else:
            raise KeyError(item_id)
        return f"/{group}/{item_id}"

    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
        def respond(status: str, body: bytes, extra: list[tuple[str, str]] | None = None) -> list[bytes]:
            headers = _HEADERS + [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))]
            if self.origin.startswith("https://"):
                headers.append(("Strict-Transport-Security", "max-age=31536000"))
            start_response(status, headers + (extra or []))
            return [body]
        def redirect(location: str, cookie: str | None = None) -> list[bytes]:
            extras = [("Location", location)]
            if cookie is not None:
                extras.append(("Set-Cookie", cookie))
            return respond("303 See Other", b"", extras)
        if environ.get("HTTP_HOST", "") not in self.allowed_hosts:
            return respond("403 Forbidden", self._page("访问被拒绝", "<p>无效的主机地址。</p>"))
        if method not in {"GET", "POST"}:
            return respond("405 Method Not Allowed", self._page("不支持的请求", ""))
        cookies = SimpleCookie()
        try:
            cookies.load(environ.get("HTTP_COOKIE", ""))
            session = cookies["personal_ai_mobile_session"].value if "personal_ai_mobile_session" in cookies else ""
        except Exception:
            session = ""
        if method == "POST" and not self._origin_ok(environ):
            return respond("403 Forbidden", self._page("访问被拒绝", "<p>来源校验失败。</p>"))
        if method == "POST" and path == "/session":
            remote = environ.get("REMOTE_ADDR", "")
            if not self._rate("login:" + remote, 10):
                return respond("429 Too Many Requests", self._page("请求过多", ""))
            try:
                values = self._body(environ)
                new_session = self.service.open_remote_session(values["token"])
            except (ValueError, KeyError, PermissionError):
                return respond("401 Unauthorized", self._login(True))
            return redirect("/dashboard", "personal_ai_mobile_session=" + new_session +
                            "; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=43200")
        if not session:
            return respond("200 OK" if method == "GET" and path == "/" else "401 Unauthorized",
                           self._login(method != "GET" or path != "/"))
        try:
            self.service.authenticate_remote_session(session)
        except PermissionError:
            return respond("401 Unauthorized", self._login(True), [
                ("Set-Cookie", "personal_ai_mobile_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0")])
        if not self._rate("session:" + hashlib.sha256(session.encode()).hexdigest() + ":" + method,
                          30 if method == "POST" else 90):
            return respond("429 Too Many Requests", self._page("请求过多", "", self._csrf(session)))
        csrf = self._csrf(session)
        if method == "POST":
            try:
                values = self._body(environ)
            except ValueError:
                return respond("400 Bad Request", self._page("表单错误", "", csrf))
            if not hmac.compare_digest(values.get("csrf", ""), csrf):
                return respond("403 Forbidden", self._page("访问被拒绝", "<p>表单安全校验失败。</p>", csrf))
            if path == "/logout":
                self.service.close_remote_session(session)
                return redirect("/", "personal_ai_mobile_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0")
            match = _ID_PATH.fullmatch(path)
            if not match or not match.group(3):
                return respond("404 Not Found", self._page("未找到操作", "", csrf))
            try:
                destination = self._mutate(*match.groups(), values)
                return redirect(destination)
            except (ValueError, KeyError, TypeError, ValidationError) as exc:
                code = "409 Conflict" if isinstance(exc, ValueError) and any(
                    word in str(exc) for word in ("stale", "conflict", "already", "not_waiting", "nonpending")) else "400 Bad Request"
                return respond(code, self._page("操作未完成", _error_detail(exc), csrf))
            except Exception:
                return respond("502 Bad Gateway", self._page("外部操作结果需核对", "<p>请查看原草案状态；不要重复发送未知结果。</p>", csrf))
        if path == "/":
            return redirect("/dashboard")
        try:
            if path == "/dashboard":
                body = self._dashboard(csrf)
            else:
                match = _ID_PATH.fullmatch(path)
                if not match or match.group(3):
                    raise KeyError(path)
                group, item_id, _ = match.groups()
                render = {"tasks": self._task, "plans": self._plan, "daily": self._daily,
                          "memory": self._memory, "external": self._external}[group]
                body = render(item_id, csrf)
            return respond("200 OK", body)
        except (KeyError, ValueError):
            return respond("404 Not Found", self._page("未找到页面", "", csrf))
