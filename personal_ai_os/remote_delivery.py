"""Optional, durable per-device delivery of existing local reminder records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from .storage import Repository


class RemoteNotificationKnownFailure(Exception):
    """Transport confirms the message was not delivered."""


class RemoteNotifier(Protocol):
    def send(self, device_id: str, title: str, body: str, idempotency_key: str) -> None: ...


def _utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("timezone_aware_datetime_required")
    return value.astimezone(timezone.utc)


def _stamp(now: datetime | None = None) -> str:
    return _utc(now).isoformat()


class RemoteDeliveryDispatcher:
    def __init__(self, repository: Repository, notifier: RemoteNotifier):
        self.repository = repository
        self.notifier = notifier

    def list_deliveries(self) -> list[dict]:
        with self.repository.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,notification_id,device_id,status,error_code,created_at_utc,updated_at_utc "
                "FROM remote_deliveries ORDER BY created_at_utc DESC,id DESC"
            )]

    def dispatch(self, now: datetime | None = None, *, max_items: int = 100) -> int:
        instant = _utc(now)
        stamp = _stamp(instant)
        with self.repository.transaction() as conn:
            expired = conn.execute(
                "SELECT id FROM remote_deliveries WHERE status='attempting' AND lease_until_utc<=?",
                (stamp,),
            ).fetchall()
            conn.execute(
                "UPDATE remote_deliveries SET status='unknown',error_code='interrupted_attempt',"
                "lease_until_utc=NULL,updated_at_utc=? "
                "WHERE status='attempting' AND lease_until_utc<=?", (stamp, stamp),
            )
            # Only reminders created after the user's per-device opt-in can be delivered.
            rows = conn.execute(
                "SELECT n.id AS notification_id,n.status AS notification_status,d.id AS device_id "
                "FROM notifications n CROSS JOIN remote_devices d "
                "WHERE d.status='active' AND d.notifications_enabled=1 "
                "AND d.expires_at_utc>? AND n.visible_at_utc>=d.notifications_enabled_at_utc "
                "AND n.visible_at_utc<=?",
                (stamp, stamp),
            ).fetchall()
            for row in rows:
                status = "missed" if row["notification_status"] == "missed" else "pending"
                conn.execute(
                    "INSERT OR IGNORE INTO remote_deliveries "
                    "(id,notification_id,device_id,status,idempotency_key,created_at_utc,updated_at_utc) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (uuid4().hex, row["notification_id"], row["device_id"], status,
                     uuid4().hex, stamp, stamp),
                )
        for row in expired:
            self.repository.append_trace("remote_delivery_unknown",
                                         {"delivery_id": row["id"], "reason": "interrupted_attempt"})

        count = 0
        while count < max_items:
            with self.repository.transaction() as conn:
                row = conn.execute(
                    "SELECT r.*,n.title,n.body FROM remote_deliveries r "
                    "JOIN notifications n ON n.id=r.notification_id "
                    "JOIN remote_devices d ON d.id=r.device_id "
                    "WHERE r.status='pending' AND d.status='active' AND d.notifications_enabled=1 "
                    "AND d.expires_at_utc>? ORDER BY r.created_at_utc,r.id LIMIT 1", (stamp,),
                ).fetchone()
                if row is None:
                    break
                conn.execute(
                    "UPDATE remote_deliveries SET status='attempting',lease_until_utc=?,"
                    "updated_at_utc=? WHERE id=?",
                    (_stamp(instant + timedelta(minutes=5)), stamp, row["id"]),
                )
                item = dict(row)
            # Revocation may race with a claimed delivery. Recheck immediately before send.
            with self.repository.connection() as conn:
                device = conn.execute(
                    "SELECT status,notifications_enabled,expires_at_utc FROM remote_devices WHERE id=?",
                    (item["device_id"],),
                ).fetchone()
            if (not device or device["status"] != "active" or not device["notifications_enabled"]
                    or device["expires_at_utc"] <= stamp):
                result, error = "cancelled", "device_disabled"
            else:
                try:
                    self.notifier.send(item["device_id"], item["title"], item["body"],
                                       item["idempotency_key"])
                    result, error = "delivered", None
                except RemoteNotificationKnownFailure:
                    result, error = "failed", "known_delivery_failure"
                except TimeoutError:
                    result, error = "unknown", "delivery_timeout"
                except Exception:
                    result, error = "unknown", "delivery_result_unknown"
            with self.repository.transaction() as conn:
                updated = conn.execute(
                    "UPDATE remote_deliveries SET status=?,error_code=?,lease_until_utc=NULL,"
                    "updated_at_utc=? WHERE id=? AND status='attempting'",
                    (result, error, stamp, item["id"]),
                ).rowcount
            self.repository.append_trace("remote_delivery_" + (result if updated else "late_result"),
                                         {"delivery_id": item["id"], "error_code": error})
            count += 1
        return count
