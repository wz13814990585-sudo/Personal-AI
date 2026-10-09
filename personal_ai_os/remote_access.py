"""Local device enrollment and authenticated read-only snapshots.

No listener or remote identity provider is enabled by this module.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .storage import Repository


def _utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("timezone_aware_datetime_required")
    return value.astimezone(timezone.utc)


def _stamp(now: datetime | None = None) -> str:
    return _utc(now).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public(row: Any) -> dict[str, Any]:
    item = dict(row)
    item.pop("token_hash", None)
    return item


class DeviceAccess:
    def __init__(self, repository: Repository):
        self.repository = repository

    def create_device(self, name: str, *, ttl_days: int = 30,
                      now: datetime | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("invalid_device_name")
        if not 1 <= ttl_days <= 90:
            raise ValueError("invalid_device_lifetime")
        instant = _utc(now)
        device_id = uuid4().hex
        token = "pad_" + secrets.token_urlsafe(32)
        with self.repository.transaction() as conn:
            conn.execute(
                "INSERT INTO remote_devices "
                "(id,name,token_hash,expires_at_utc,created_at_utc,updated_at_utc) "
                "VALUES (?,?,?,?,?,?)",
                (device_id, name.strip(), _hash(token), _stamp(instant + timedelta(days=ttl_days)),
                 _stamp(instant), _stamp(instant)),
            )
        self.repository.append_trace("remote_device_created", {"device_id": device_id}, actor="user")
        return {"device": self.get_device(device_id), "token": token}

    def list_devices(self) -> list[dict[str, Any]]:
        with self.repository.connection() as conn:
            return [_public(row) for row in conn.execute(
                "SELECT * FROM remote_devices ORDER BY created_at_utc DESC,id DESC"
            )]

    def get_device(self, device_id: str) -> dict[str, Any]:
        with self.repository.connection() as conn:
            row = conn.execute("SELECT * FROM remote_devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise KeyError(device_id)
        return _public(row)

    def revoke_device(self, device_id: str, version: int,
                      now: datetime | None = None) -> dict[str, Any]:
        instant = _stamp(now)
        with self.repository.transaction() as conn:
            changed = conn.execute(
                "UPDATE remote_devices SET status='revoked',notifications_enabled=0,"
                "notifications_enabled_at_utc=NULL,version=version+1,updated_at_utc=? "
                "WHERE id=? AND version=? AND status='active'",
                (instant, device_id, version),
            ).rowcount
            if not changed:
                raise ValueError("stale_or_revoked_device")
            conn.execute("DELETE FROM remote_sessions WHERE device_id=?", (device_id,))
            conn.execute("UPDATE remote_deliveries SET status='cancelled',updated_at_utc=? "
                         "WHERE device_id=? AND status='pending'", (instant, device_id))
        self.repository.append_trace("remote_device_revoked", {"device_id": device_id}, actor="user")
        return self.get_device(device_id)

    def set_notifications(self, device_id: str, version: int, enabled: bool,
                          now: datetime | None = None) -> dict[str, Any]:
        instant = _stamp(now)
        with self.repository.transaction() as conn:
            row = conn.execute("SELECT notifications_enabled,status,expires_at_utc,"
                               "notifications_enabled_at_utc FROM remote_devices "
                               "WHERE id=? AND version=?", (device_id, version)).fetchone()
            if row is None or row["status"] != "active" or row["expires_at_utc"] <= instant:
                raise ValueError("stale_or_revoked_device")
            enabled_at = (instant if enabled and not row["notifications_enabled"] else
                          None if not enabled else row["notifications_enabled_at_utc"])
            conn.execute(
                "UPDATE remote_devices SET notifications_enabled=?,notifications_enabled_at_utc=?,"
                "version=version+1,updated_at_utc=? WHERE id=?",
                (int(enabled), enabled_at, instant, device_id),
            )
            if not enabled:
                conn.execute("UPDATE remote_deliveries SET status='cancelled',updated_at_utc=? "
                             "WHERE device_id=? AND status='pending'", (instant, device_id))
        self.repository.append_trace("remote_notifications_changed",
                                     {"device_id": device_id, "enabled": bool(enabled)}, actor="user")
        return self.get_device(device_id)

    def open_session(self, device_token: str, *, now: datetime | None = None) -> str:
        instant = _utc(now)
        if not isinstance(device_token, str) or not device_token.startswith("pad_") or len(device_token) > 128:
            raise PermissionError("device_authentication_required")
        digest = _hash(device_token)
        with self.repository.transaction() as conn:
            row = conn.execute("SELECT id,status,expires_at_utc,token_hash FROM remote_devices "
                               "WHERE token_hash=?", (digest,)).fetchone()
            if (row is None or not hmac.compare_digest(row["token_hash"], digest)
                    or row["status"] != "active" or row["expires_at_utc"] <= _stamp(instant)):
                raise PermissionError("device_authentication_required")
            session = "pas_" + secrets.token_urlsafe(32)
            conn.execute("INSERT INTO remote_sessions VALUES (?,?,?,?,?)",
                         (uuid4().hex, row["id"], _hash(session),
                          _stamp(instant + timedelta(hours=12)), _stamp(instant)))
            conn.execute("UPDATE remote_devices SET last_used_at_utc=? WHERE id=?",
                         (_stamp(instant), row["id"]))
        return session

    def authenticate_session(self, session_token: str, *, now: datetime | None = None) -> str:
        instant = _stamp(now)
        if not isinstance(session_token, str) or not session_token.startswith("pas_") or len(session_token) > 128:
            raise PermissionError("device_authentication_required")
        digest = _hash(session_token)
        with self.repository.connection() as conn:
            row = conn.execute(
                "SELECT s.device_id,s.token_hash,s.expires_at_utc,d.status,d.expires_at_utc AS device_expires "
                "FROM remote_sessions s JOIN remote_devices d ON d.id=s.device_id "
                "WHERE s.token_hash=?", (digest,),
            ).fetchone()
        if (row is None or not hmac.compare_digest(row["token_hash"], digest)
                or row["expires_at_utc"] <= instant or row["device_expires"] <= instant
                or row["status"] != "active"):
            raise PermissionError("device_authentication_required")
        return row["device_id"]

    def close_session(self, session_token: str) -> None:
        if not isinstance(session_token, str):
            return
        with self.repository.transaction() as conn:
            conn.execute("DELETE FROM remote_sessions WHERE token_hash=?", (_hash(session_token),))

    def snapshot(self, session_token: str, *, now: datetime | None = None) -> dict[str, Any]:
        device_id = self.authenticate_session(session_token, now=now)
        with self.repository.connection() as conn:
            tasks = [dict(row) for row in conn.execute(
                "SELECT id,title,status,priority,start_at_utc,due_at_utc FROM tasks "
                "WHERE status!='completed' ORDER BY COALESCE(start_at_utc,due_at_utc,'9999'),id LIMIT 100"
            )]
            notices = [dict(row) for row in conn.execute(
                "SELECT id,title,body,status,visible_at_utc FROM notifications "
                "ORDER BY visible_at_utc DESC,id DESC LIMIT 50"
            )]
        return {"device_id": device_id, "generated_at_utc": _stamp(now),
                "tasks": tasks, "notifications": notices}
