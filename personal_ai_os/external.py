"""Constrained external capability directory and per-item approval ledger."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol
from uuid import uuid4

from .storage import Repository


BUILTIN_OPERATIONS = {
    "calendar": {"list_events": "read", "create_event": "write", "update_event": "write"},
    "mail": {"list_messages": "read", "get_message": "read", "send_message": "write"},
}
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_SENSITIVE = {"api_key", "token", "access_token", "refresh_token", "password", "secret", "authorization"}


class ExternalKnownFailure(Exception):
    """Adapter guarantees no external write occurred."""


class ExternalUnknownResult(Exception):
    """Adapter cannot establish whether the external write occurred."""


class CalendarAdapter(Protocol):
    def list_events(self, account_id: str, query: dict[str, Any]) -> list[dict[str, Any]]: ...
    def create_event(self, account_id: str, calendar_id: str, event: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...
    def update_event(self, account_id: str, event_id: str, changes: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...


class MailAdapter(Protocol):
    def list_messages(self, account_id: str, query: dict[str, Any]) -> list[dict[str, Any]] | dict[str, Any]: ...
    def get_message(self, account_id: str, message_id: str) -> list[dict[str, Any]]: ...
    def send_message(self, account_id: str, recipient: str, message: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...


class McpAdapter(Protocol):
    # Code-owned capabilities must be a superset of the user's directory allowlist.
    operations: Mapping[str, str]
    def read(self, operation: str, account_id: str, query: dict[str, Any]) -> list[dict[str, Any]]: ...
    def write(self, operation: str, account_id: str, target: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > 131072:
        raise ValueError("external_payload_too_large")
    return encoded


def _clean(value: Any) -> None:
    if isinstance(value, dict):
        if any(str(key).lower() in _SENSITIVE for key in value):
            raise ValueError("external_credentials_not_allowed")
        for nested in value.values():
            _clean(nested)
    elif isinstance(value, list):
        for nested in value:
            _clean(nested)


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError(f"invalid_{field}")
    return value.strip()


def _payload(kind: str, operation: str, value: dict[str, Any], *,
             prepared: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid_external_payload")
    _clean(value)
    _json(value)
    if kind == "calendar" and operation in {"create_event", "update_event"}:
        allowed = {"title", "start_at_utc", "end_at_utc", "description", "location",
                   "reminder_minutes"}
        if prepared and operation == "update_event":
            allowed.update({"expected_event_fingerprint", "selected_calendar_id"})
        if not value or set(value) - allowed:
            raise ValueError("invalid_calendar_payload")
        if operation == "create_event" and not {"title", "start_at_utc", "end_at_utc"} <= set(value):
            raise ValueError("invalid_calendar_payload")
        if "title" in value and not str(value["title"]).strip():
            raise ValueError("invalid_calendar_payload")
        for field in value:
            if field == "reminder_minutes":
                if value[field] is not None and (type(value[field]) is not int
                                                 or not 0 <= value[field] <= 10080):
                    raise ValueError("invalid_reminder_minutes")
                continue
            if field == "expected_event_fingerprint":
                if not isinstance(value[field], str) or not re.fullmatch(r"[0-9a-f]{64}", value[field]):
                    raise ValueError("invalid_event_fingerprint")
                continue
            if not isinstance(value[field], str) or len(value[field]) > 10000:
                raise ValueError("invalid_calendar_payload")
        if {"start_at_utc", "end_at_utc"} <= set(value):
            try:
                start = datetime.fromisoformat(value["start_at_utc"])
                end = datetime.fromisoformat(value["end_at_utc"])
                if start.tzinfo is None or end.tzinfo is None or start >= end:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("invalid_calendar_time") from exc
    if kind == "mail" and operation == "send_message":
        if set(value) != {"subject", "body"} or any(
            not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 50000
            for key in value
        ) or "\r" in value["subject"] or "\n" in value["subject"]:
            raise ValueError("invalid_mail_payload")
    return value


class ExternalGateway:
    def __init__(self, repository: Repository, adapters: Mapping[str, object] | None = None):
        self.repository = repository
        self.adapters = dict(adapters or {})

    def _audit(self, server_id: str | None, operation: str | None, event: str,
               detail: dict[str, Any] | None = None, proposal_id: str | None = None,
               connection: Any = None) -> None:
        values = (uuid4().hex, server_id, operation, proposal_id, event,
                  _json(detail or {}), _now())
        sql = "INSERT INTO external_audit VALUES (?,?,?,?,?,?,?)"
        if connection is not None:
            connection.execute(sql, values)
        else:
            with self.repository.transaction() as conn:
                conn.execute(sql, values)

    def list_servers(self) -> list[dict[str, Any]]:
        with self.repository.connection() as conn:
            rows = conn.execute("SELECT * FROM mcp_servers ORDER BY created_at_utc,id").fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["operations"] = [dict(op) for op in conn.execute(
                    "SELECT operation,access,enabled FROM mcp_operations WHERE server_id=? ORDER BY operation",
                    (item["id"],),
                )]
                item["bound"] = item["id"] in self.adapters
                result.append(item)
            return result

    def register_server(self, name: str, kind: str,
                        operations: Mapping[str, str] | None = None) -> dict[str, Any]:
        name = _text(name, "server_name")
        if kind not in {"calendar", "mail", "mcp"}:
            raise ValueError("unsupported_server_kind")
        if kind == "mcp" and not isinstance(operations, Mapping):
            raise ValueError("invalid_mcp_operations")
        choices = BUILTIN_OPERATIONS.get(kind, operations or {})
        if kind == "mcp" and (not choices or len(choices) > 32):
            raise ValueError("invalid_mcp_operations")
        if any(not isinstance(op, str) or not _NAME.fullmatch(op) or access not in {"read", "write"}
               for op, access in choices.items()):
            raise ValueError("invalid_mcp_operations")
        server_id, now = uuid4().hex, _now()
        with self.repository.transaction() as conn:
            conn.execute("INSERT INTO mcp_servers VALUES (?,?,?,?,?,?,?)",
                         (server_id, name, kind, "paused", 1, now, now))
            conn.executemany("INSERT INTO mcp_operations VALUES (?,?,?,0)",
                             [(server_id, op, access) for op, access in choices.items()])
            self._audit(server_id, None, "server_registered", {"kind": kind}, connection=conn)
        return next(item for item in self.list_servers() if item["id"] == server_id)

    def set_server_status(self, server_id: str, version: int, active: bool) -> dict[str, Any]:
        with self.repository.transaction() as conn:
            changed = conn.execute(
                "UPDATE mcp_servers SET status=?,version=version+1,updated_at_utc=? "
                "WHERE id=? AND version=?",
                ("active" if active else "paused", _now(), server_id, version),
            ).rowcount
            if not changed:
                raise ValueError("stale_or_missing_mcp_server")
            self._audit(server_id, None, "server_status_changed", {"active": active}, connection=conn)
        return next(item for item in self.list_servers() if item["id"] == server_id)

    def set_operation_enabled(self, server_id: str, operation: str, version: int,
                              enabled: bool) -> dict[str, Any]:
        with self.repository.transaction() as conn:
            changed = conn.execute(
                "UPDATE mcp_servers SET version=version+1,updated_at_utc=? WHERE id=? AND version=?",
                (_now(), server_id, version),
            ).rowcount
            if not changed:
                raise ValueError("stale_or_missing_mcp_server")
            changed = conn.execute(
                "UPDATE mcp_operations SET enabled=? WHERE server_id=? AND operation=?",
                (int(enabled), server_id, operation),
            ).rowcount
            if not changed:
                raise ValueError("unknown_mcp_operation")
            self._audit(server_id, operation, "operation_status_changed",
                        {"enabled": enabled}, connection=conn)
        return next(item for item in self.list_servers() if item["id"] == server_id)

    def _authorized(self, server_id: str, operation: str, access: str) -> tuple[str, object]:
        with self.repository.connection() as conn:
            row = conn.execute(
                "SELECT s.kind,s.status,o.access,o.enabled FROM mcp_servers s "
                "JOIN mcp_operations o ON o.server_id=s.id WHERE s.id=? AND o.operation=?",
                (server_id, operation),
            ).fetchone()
        if not row or row["status"] != "active" or row["access"] != access or not row["enabled"]:
            self._audit(server_id, operation, "operation_denied", {"reason": "not_allowed"})
            raise PermissionError("external_operation_not_allowed")
        adapter = self.adapters.get(server_id)
        if adapter is None:
            self._audit(server_id, operation, "operation_denied", {"reason": "adapter_unavailable"})
            raise RuntimeError("external_adapter_unavailable")
        if row["kind"] == "mcp" and getattr(adapter, "operations", {}).get(operation) != access:
            self._audit(server_id, operation, "operation_denied", {"reason": "code_capability_missing"})
            raise PermissionError("external_code_capability_missing")
        return row["kind"], adapter

    def read(self, server_id: str, operation: str, account_id: str,
             query: dict[str, Any]) -> dict[str, Any]:
        account_id = _text(account_id, "account_id")
        _payload("mcp", operation, query)
        kind, adapter = self._authorized(server_id, operation, "read")
        try:
            if kind == "calendar":
                result = adapter.list_events(account_id, query)
            elif kind == "mail":
                if operation == "get_message":
                    result = adapter.get_message(account_id, query.get("message_id"))
                else:
                    result = adapter.list_messages(account_id, query)
            else:
                result = adapter.read(operation, account_id, query)
            next_page_token = ""
            if kind == "mail" and operation == "list_messages" and isinstance(result, dict):
                next_page_token = result.get("next_page_token", "")
                result = result.get("items")
                if not isinstance(next_page_token, str) or len(next_page_token) > 1000:
                    raise ValueError("invalid_external_response")
            if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                raise ValueError("invalid_external_response")
            _json(result)
        except Exception as exc:
            self._audit(server_id, operation, "read_failed", {"error_code": type(exc).__name__})
            raise
        self._audit(server_id, operation, "read_succeeded", {"count": len(result)})
        response = {"source": {"server_id": server_id, "operation": operation,
                               "account_id": account_id}, "items": result}
        if kind == "mail" and operation == "list_messages":
            response["next_page_token"] = next_page_token
        return response

    def propose_write(self, server_id: str, operation: str, account_id: str,
                      target: str, payload: dict[str, Any]) -> dict[str, Any]:
        account_id, target = _text(account_id, "account_id"), _text(target, "target")
        kind, adapter = self._authorized(server_id, operation, "write")
        _payload(kind, operation, payload)
        if kind == "calendar" and hasattr(adapter, "prepare_write"):
            payload = adapter.prepare_write(operation, account_id, target, payload)
            _payload(kind, operation, payload, prepared=True)
        if kind == "mail" and operation == "send_message" and not re.fullmatch(
            r"[^@\s<>;,]+@[^@\s<>;,]+\.[^@\s<>;,]+", target
        ):
            raise ValueError("invalid_mail_recipient")
        proposal_id, key, now = uuid4().hex, uuid4().hex, _now()
        existing_id = None
        with self.repository.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM external_write_proposals WHERE server_id=? AND operation=? "
                "AND account_id=? AND target=? AND payload_json=? "
                "AND status IN ('pending','attempting','timeout','unknown') ORDER BY created_at_utc DESC LIMIT 1",
                (server_id, operation, account_id, target, _json(payload)),
            ).fetchone()
            if existing:
                existing_id = existing["id"]
                self._audit(server_id, operation, "write_proposal_reused",
                            {"status": "unresolved"}, existing_id, conn)
            else:
                conn.execute(
                    "INSERT INTO external_write_proposals "
                    "(id,server_id,operation,account_id,target,payload_json,status,revision,"
                    "idempotency_key,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,'pending',0,?,?,?)",
                    (proposal_id, server_id, operation, account_id, target, _json(payload), key, now, now),
                )
                self._audit(server_id, operation, "write_proposed",
                            {"target": target}, proposal_id, conn)
        return self.get_proposal(existing_id or proposal_id)

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self.repository.connection() as conn:
            row = conn.execute("SELECT * FROM external_write_proposals WHERE id=?",
                               (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
        # The key is sent only to a code-bound adapter; the UI does not need it.
        item.pop("idempotency_key")
        return item

    def list_proposals(self) -> list[dict[str, Any]]:
        with self.repository.connection() as conn:
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM external_write_proposals ORDER BY created_at_utc DESC,id DESC"
            )]
        return [self.get_proposal(item) for item in ids]

    def reject_write(self, proposal_id: str, revision: int) -> dict[str, Any]:
        with self.repository.transaction() as conn:
            row = conn.execute("SELECT server_id,operation FROM external_write_proposals "
                               "WHERE id=? AND revision=? AND status='pending'",
                               (proposal_id, revision)).fetchone()
            if row is None:
                raise ValueError("stale_or_nonpending_external_proposal")
            conn.execute("UPDATE external_write_proposals SET status='rejected',revision=revision+1,"
                         "updated_at_utc=? WHERE id=?", (_now(), proposal_id))
            self._audit(row["server_id"], row["operation"], "write_rejected",
                        proposal_id=proposal_id, connection=conn)
        return self.get_proposal(proposal_id)

    def approve_write(self, proposal_id: str, revision: int) -> dict[str, Any]:
        denial: Exception | None = None
        with self.repository.transaction() as conn:
            row = conn.execute("SELECT * FROM external_write_proposals WHERE id=?",
                               (proposal_id,)).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["revision"] == revision + 1 and row["status"] in {
                "attempting", "succeeded", "failed", "timeout", "unknown"
            }:
                return self.get_proposal(proposal_id)
            if row["revision"] != revision or row["status"] != "pending":
                raise ValueError("stale_or_nonpending_external_proposal")
            # Recheck at approval, including a code-bound adapter, before recording an attempt.
            # This inline query avoids a nested transaction for the authorization read.
            scope = conn.execute(
                "SELECT s.kind,s.status,o.access,o.enabled FROM mcp_servers s JOIN mcp_operations o "
                "ON o.server_id=s.id WHERE s.id=? AND o.operation=?",
                (row["server_id"], row["operation"]),
            ).fetchone()
            adapter = self.adapters.get(row["server_id"])
            if not scope or scope["status"] != "active" or scope["access"] != "write" or not scope["enabled"]:
                denial = PermissionError("external_operation_not_allowed")
            elif adapter is None or (scope["kind"] == "mcp" and
                                     getattr(adapter, "operations", {}).get(row["operation"]) != "write"):
                denial = RuntimeError("external_adapter_unavailable")
            else:
                lease = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                conn.execute("UPDATE external_write_proposals SET status='attempting',revision=revision+1,"
                             "lease_until_utc=?,updated_at_utc=? WHERE id=?", (lease, _now(), proposal_id))
                self._audit(row["server_id"], row["operation"], "write_attempt_started",
                            {"target": row["target"]}, proposal_id, conn)
                data = dict(row)
        if denial is not None:
            self._audit(row["server_id"], row["operation"], "write_approval_denied",
                        {"reason": str(denial)}, proposal_id)
            raise denial
        payload = json.loads(data["payload_json"])
        try:
            if scope["kind"] == "calendar" and data["operation"] == "create_event":
                result = adapter.create_event(data["account_id"], data["target"], payload,
                                              data["idempotency_key"])
            elif scope["kind"] == "calendar":
                result = adapter.update_event(data["account_id"], data["target"], payload,
                                              data["idempotency_key"])
            elif scope["kind"] == "mail":
                result = adapter.send_message(data["account_id"], data["target"], payload,
                                              data["idempotency_key"])
            else:
                result = adapter.write(data["operation"], data["account_id"], data["target"],
                                       payload, data["idempotency_key"])
            if not isinstance(result, dict):
                raise ExternalUnknownResult("invalid_external_response")
            _json(result)
            status, error = "succeeded", None
        except ExternalKnownFailure as exc:
            code = str(exc)
            status, error, result = "failed", code if _NAME.fullmatch(code) else type(exc).__name__, None
        except TimeoutError as exc:
            code = str(exc)
            status, error, result = "timeout", code if _NAME.fullmatch(code) else type(exc).__name__, None
        except ExternalUnknownResult as exc:
            code = str(exc)
            status, error, result = "unknown", code if _NAME.fullmatch(code) else type(exc).__name__, None
        except Exception as exc:
            status, error, result = "unknown", type(exc).__name__, None
        with self.repository.transaction() as conn:
            conn.execute(
                "UPDATE external_write_proposals SET status=?,result_json=?,error_code=?,"
                "lease_until_utc=NULL,updated_at_utc=? WHERE id=? AND status='attempting'",
                (status, _json(result) if result is not None else None, error, _now(), proposal_id),
            )
            self._audit(data["server_id"], data["operation"], "write_" + status,
                        {"error_code": error}, proposal_id, conn)
        return self.get_proposal(proposal_id)

    def recover_expired_attempts(self, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self.repository.transaction() as conn:
            rows = conn.execute(
                "SELECT id,server_id,operation FROM external_write_proposals "
                "WHERE status='attempting' AND lease_until_utc<=?", (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute("UPDATE external_write_proposals SET status='unknown',"
                             "error_code='interrupted_attempt',lease_until_utc=NULL,updated_at_utc=? "
                             "WHERE id=?", (_now(), row["id"]))
                self._audit(row["server_id"], row["operation"], "write_unknown",
                            {"error_code": "interrupted_attempt"}, row["id"], conn)
        return len(rows)

    def list_audit(self) -> list[dict[str, Any]]:
        with self.repository.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM external_audit ORDER BY created_at_utc DESC,id DESC"
            )]
