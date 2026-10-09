"""Independent SQLite-backed local worker. No Agent or Web session is required."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from .daily_planning import propose_daily_plan
from .proactive import scan_suggestions
from .recurrence import generate_instances, local_start
from .remote_delivery import RemoteDeliveryDispatcher, RemoteNotifier
from .storage import Repository, _rows


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone_aware_datetime_required")
    return value.astimezone(timezone.utc)


def stamp(value: datetime) -> str:
    return utc(value).isoformat()


def quiet_end(due: datetime, zone: ZoneInfo, start: str, end: str) -> datetime:
    """Defer a future reminder in the quiet interval to the next local end."""
    due = utc(due)
    start_clock = clock_time.fromisoformat(start)
    end_clock = clock_time.fromisoformat(end)
    if start_clock == end_clock:
        return due
    local = due.astimezone(zone)
    wall = local.time().replace(tzinfo=None)
    inside = (wall >= start_clock or wall < end_clock) if start_clock > end_clock else (
        start_clock <= wall < end_clock
    )
    if not inside:
        return due
    end_day = local.date() + timedelta(days=1 if start_clock > end_clock and wall >= start_clock else 0)
    # A DST gap or fold cannot be a reliable delivery instant. Search forward minute by minute.
    for offset in range(181):
        naive = datetime.combine(end_day, end_clock) + timedelta(minutes=offset)
        result, error = local_start(naive.date(), naive.time(), zone)
        if error is None and result is not None:
            return utc(result)
    raise ValueError("quiet_end_has_no_valid_instant")


class SystemNotifier(Protocol):
    def supported(self) -> bool: ...
    def send(self, title: str, body: str) -> None: ...


class LocalSystemNotifier:
    def supported(self) -> bool:
        return platform.system() == "Darwin" and shutil.which("osascript") is not None

    def send(self, title: str, body: str) -> None:
        if not self.supported():
            raise RuntimeError("system_notifications_unsupported")
        def escaped(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        script = f'display notification "{escaped(body)}" with title "{escaped(title)}"'
        subprocess.run(["osascript", "-e", script], check=True, timeout=10, capture_output=True)


def set_task_reminder(repository: Repository, task_id: str, lead_minutes: int, enabled: bool = True) -> dict:
    if not 0 <= lead_minutes <= 10080:
        raise ValueError("invalid_reminder_lead_minutes")
    with repository.transaction() as connection:
        task = connection.execute("SELECT id,start_at_utc FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(task_id)
        if enabled and not task["start_at_utc"]:
            raise ValueError("reminder_requires_scheduled_task")
        connection.execute(
            "INSERT INTO task_reminders(task_id,lead_minutes,enabled) VALUES (?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET lead_minutes=excluded.lead_minutes,enabled=excluded.enabled",
            (task_id, lead_minutes, int(enabled)),
        )
    return get_task_reminder(repository, task_id)


def get_task_reminder(repository: Repository, task_id: str) -> dict | None:
    with repository.connection() as connection:
        row = connection.execute("SELECT * FROM task_reminders WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_notifications(repository: Repository) -> list[dict]:
    with repository.connection() as connection:
        return _rows(connection.execute("SELECT * FROM notifications ORDER BY visible_at_utc DESC"))


def mark_notification_read(repository: Repository, notification_id: str, now: datetime | None = None) -> dict:
    with repository.transaction() as connection:
        if connection.execute(
            "UPDATE notifications SET read_at_utc=COALESCE(read_at_utc,?) WHERE id=?",
            (stamp(now or datetime.now(timezone.utc)), notification_id),
        ).rowcount == 0:
            raise KeyError(notification_id)
        row = connection.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
        return dict(row)


def list_daily_reviews(repository: Repository) -> list[dict]:
    with repository.connection() as connection:
        rows = _rows(connection.execute("SELECT * FROM daily_reviews ORDER BY local_date DESC"))
    return [{**row, "snapshot": json.loads(row["snapshot_json"])} for row in rows]


def save_daily_review_note(repository: Repository, review_id: str, revision: int, note: str) -> dict:
    if len(note) > 10000:
        raise ValueError("review_note_too_long")
    with repository.transaction() as connection:
        cursor = connection.execute(
            "UPDATE daily_reviews SET note=?,revision=revision+1,updated_at_utc=? "
            "WHERE id=? AND revision=?", (note, stamp(datetime.now(timezone.utc)), review_id, revision),
        )
        if cursor.rowcount == 0:
            if connection.execute("SELECT 1 FROM daily_reviews WHERE id=?", (review_id,)).fetchone():
                raise ValueError("stale_review_revision")
            raise KeyError(review_id)
        row = connection.execute("SELECT * FROM daily_reviews WHERE id=?", (review_id,)).fetchone()
    return {**dict(row), "snapshot": json.loads(row["snapshot_json"])}


def create_daily_review(repository: Repository, day: date, timezone_name: str, now: datetime) -> dict:
    zone = ZoneInfo(timezone_name)
    with repository.transaction() as connection:
        existing = connection.execute(
            "SELECT * FROM daily_reviews WHERE local_date=? AND timezone=?", (day.isoformat(), timezone_name)
        ).fetchone()
        if existing:
            return {**dict(existing), "snapshot": json.loads(existing["snapshot_json"])}
        tasks = _rows(connection.execute("SELECT id,status,start_at_utc,due_at_utc FROM tasks"))
        relevant = [row for row in tasks if any(
            row[key] and datetime.fromisoformat(row[key]).astimezone(zone).date() == day
            for key in ("start_at_utc", "due_at_utc")
        )]
        habits = _rows(connection.execute("SELECT id,timezone FROM habits WHERE status='active'"))
        checked = 0
        for habit in habits:
            habit_day = day.isoformat()
            checked += bool(connection.execute(
                "SELECT 1 FROM habit_checkins WHERE habit_id=? AND local_date=? AND completed=1",
                (habit["id"], habit_day),
            ).fetchone())
        snapshot = {"completed_tasks": sum(row["status"] == "completed" for row in relevant),
                    "unfinished_tasks": sum(row["status"] != "completed" for row in relevant),
                    "active_habits": len(habits), "checked_habits": checked}
        plan = connection.execute(
            "SELECT conflicts_json FROM daily_plan_proposals WHERE local_date=? AND timezone=? "
            "ORDER BY created_at_utc DESC LIMIT 1", (day.isoformat(), timezone_name),
        ).fetchone()
        snapshot["plan_conflicts"] = len(json.loads(plan["conflicts_json"])) if plan else 0
        review_id = uuid4().hex
        connection.execute(
            "INSERT INTO daily_reviews(id,local_date,timezone,snapshot_json,note,created_at_utc,updated_at_utc) "
            "VALUES (?,?,?,?,'',?,?)",
            (review_id, day.isoformat(), timezone_name, json.dumps(snapshot), stamp(now), stamp(now)),
        )
    return {"id": review_id, "local_date": day.isoformat(), "timezone": timezone_name,
            "snapshot": snapshot, "note": "", "revision": 0}


class Worker:
    def __init__(self, repository: Repository, *, notifier: SystemNotifier | None = None,
                 remote_notifier: RemoteNotifier | None = None,
                 owner: str | None = None, lease_seconds: int = 30,
                 timezone_name: str = "Australia/Sydney"):
        self.repository = repository
        self.notifier = notifier or LocalSystemNotifier()
        self.remote_notifier = remote_notifier
        self.owner = owner or uuid4().hex
        self.lease_seconds = lease_seconds
        self.timezone_name = timezone_name

    def _enqueue(self, kind: str, key: str, payload: dict, due: datetime, now: datetime) -> None:
        with self.repository.transaction() as connection:
            connection.execute(
                "INSERT INTO scheduled_jobs(id,kind,dedupe_key,payload_json,due_at_utc,created_at_utc,updated_at_utc) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO UPDATE SET "
                "due_at_utc=CASE WHEN scheduled_jobs.status='pending' THEN excluded.due_at_utc "
                "ELSE scheduled_jobs.due_at_utc END, updated_at_utc=excluded.updated_at_utc",
                (uuid4().hex, kind, key, json.dumps(payload), stamp(due), stamp(now), stamp(now)),
            )

    def seed(self, now: datetime) -> None:
        now = utc(now)
        with self.repository.connection() as connection:
            rules = _rows(connection.execute("SELECT id,timezone FROM recurrence_rules WHERE status='active'"))
            reminders = _rows(connection.execute(
                "SELECT t.id,t.title,t.start_at_utc,t.status,r.lead_minutes FROM tasks t "
                "JOIN task_reminders r ON r.task_id=t.id WHERE r.enabled=1 AND t.start_at_utc IS NOT NULL "
                "AND t.status!='completed'"
            ))
            settings = {row["key"]: json.loads(row["value_json"]) for row in
                        connection.execute("SELECT key,value_json FROM settings")}
        for rule in rules:
            rule_zone = ZoneInfo(rule["timezone"])
            day = now.astimezone(rule_zone).date()
            midnight, error = local_start(day, clock_time(0), rule_zone)
            self._enqueue("recurrence_scan", f"recurrence:{rule['id']}:{day}",
                          {"rule_id": rule["id"], "local_date": day.isoformat()},
                          midnight if error is None else now, now)
        zone_name = settings.get("timezone", self.timezone_name)
        zone = ZoneInfo(zone_name)
        today = now.astimezone(zone).date()
        for offset in range(8):
            day = today + timedelta(days=offset)
            morning, reason = local_start(day, clock_time(7), zone)
            if reason is None:
                self._enqueue("daily_plan", f"daily-plan:{zone_name}:{day}",
                              {"local_date": day.isoformat(), "timezone": zone_name}, morning, now)
            at, reason = local_start(day, clock_time(21), zone)
            if reason is None:
                self._enqueue("daily_review", f"review:{zone_name}:{day}",
                              {"local_date": day.isoformat(), "timezone": zone_name}, at, now)
        quiet_start = settings.get("quiet_start", "22:00")
        quiet_stop = settings.get("quiet_end", "07:00")
        for task in reminders:
            original = utc(datetime.fromisoformat(task["start_at_utc"])) - timedelta(minutes=task["lead_minutes"])
            if original > now + timedelta(days=8):
                continue
            due = quiet_end(original, zone, quiet_start, quiet_stop)
            key = f"reminder:{task['id']}:{task['start_at_utc']}:{task['lead_minutes']}"
            self._enqueue("reminder", key, {"task_id": task["id"], "start_at_utc": task["start_at_utc"],
                    "original_due_at_utc": stamp(original), "lead_minutes": task["lead_minutes"]}, due, now)

    def claim(self, now: datetime) -> dict | None:
        now = utc(now)
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_jobs WHERE (status='pending' AND due_at_utc<=?) "
                "OR (status='leased' AND lease_until_utc<=?) ORDER BY due_at_utc,id LIMIT 1",
                (stamp(now), stamp(now)),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE scheduled_jobs SET status='leased',lease_owner=?,lease_until_utc=?,"
                "attempts=attempts+1,updated_at_utc=? WHERE id=?",
                (self.owner, stamp(now + timedelta(seconds=self.lease_seconds)), stamp(now), row["id"]),
            )
            return {**dict(row), "payload": json.loads(row["payload_json"])}

    def _finish(self, job_id: str, status: str, now: datetime, error: str | None = None) -> None:
        with self.repository.transaction() as connection:
            if connection.execute(
                "UPDATE scheduled_jobs SET status=?,lease_owner=NULL,lease_until_utc=NULL,"
                "last_error=?,updated_at_utc=? WHERE id=? AND status='leased' AND lease_owner=?",
                (status, error, stamp(now), job_id, self.owner),
            ).rowcount != 1:
                raise RuntimeError("worker_lease_lost")

    def _remind(self, job: dict, now: datetime) -> str:
        payload = job["payload"]
        with self.repository.transaction() as connection:
            existing = connection.execute("SELECT id FROM notifications WHERE job_id=?", (job["id"],)).fetchone()
            if existing:
                return "completed"
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (payload["task_id"],)).fetchone()
            config = connection.execute("SELECT * FROM task_reminders WHERE task_id=?", (payload["task_id"],)).fetchone()
            if (task is None or task["status"] == "completed" or config is None or not config["enabled"]
                    or task["start_at_utc"] != payload["start_at_utc"]
                    or config["lead_minutes"] != payload["lead_minutes"]):
                return "skipped"
            missed = (now > datetime.fromisoformat(job["due_at_utc"]) + timedelta(minutes=5)
                      or now >= datetime.fromisoformat(task["start_at_utc"]))
            setting = connection.execute(
                "SELECT value_json FROM settings WHERE key='system_notifications_enabled'"
            ).fetchone()
            enabled = bool(json.loads(setting["value_json"])) if setting else False
            supported = self.notifier.supported() if enabled and not missed else False
            system_status = ("missed" if missed else "disabled" if not enabled else
                             "unsupported" if not supported else "unknown")
            connection.execute(
                "INSERT INTO notifications(id,job_id,task_id,title,body,original_due_at_utc,"
                "visible_at_utc,status,system_status) VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid4().hex, job["id"], task["id"], task["title"],
                 f"任务将于 {task['start_at_utc']} 开始", payload["original_due_at_utc"],
                 stamp(now), "missed" if missed else "delivered", system_status),
            )
        # The durable 'unknown' marker is committed before the external side effect.
        # A crash or expired lease can never silently send the same system alert twice.
        if system_status == "unknown":
            try:
                self.notifier.send(task["title"], f"任务将于 {task['start_at_utc']} 开始")
                result, error = "delivered", None
            except subprocess.TimeoutExpired as exc:
                result, error = "unknown", str(exc)
            except Exception as exc:
                result, error = "failed", str(exc)
            with self.repository.transaction() as connection:
                connection.execute(
                    "UPDATE notifications SET system_status=?,error=? WHERE job_id=?",
                    (result, error, job["id"]),
                )
        return "missed" if missed else "completed"

    def process_one(self, now: datetime) -> dict | None:
        now = utc(now)
        job = self.claim(now)
        if job is None:
            return None
        try:
            if job["kind"] == "recurrence_scan":
                payload = job["payload"]
                rule = self.repository.get_recurrence_rule(payload["rule_id"])
                day = date.fromisoformat(payload["local_date"])
                today = now.astimezone(ZoneInfo(rule["timezone"])).date()
                if day < today:
                    status = "skipped"
                else:
                    generate_instances(self.repository, rule["id"], day, day, as_of_local_date=today)
                    status = "completed"
            elif job["kind"] == "daily_review":
                payload = job["payload"]
                create_daily_review(self.repository, date.fromisoformat(payload["local_date"]),
                                    payload["timezone"], now)
                try:
                    scan_suggestions(self.repository, now)
                except Exception as exc:
                    # A suggestion failure must not erase or fail an already saved P1 review.
                    self.repository.append_trace(
                        "proactive_scan_failed", {"error_type": type(exc).__name__},
                    )
                status = "completed"
            elif job["kind"] == "daily_plan":
                payload = job["payload"]
                day = date.fromisoformat(payload["local_date"])
                today = now.astimezone(ZoneInfo(payload["timezone"])).date()
                if day < today:
                    status = "skipped"
                else:
                    propose_daily_plan(
                        self.repository, day, payload["timezone"],
                        trigger_key=job["dedupe_key"], as_of=now,
                    )
                    status = "completed"
            else:
                status = self._remind(job, now)
            self._finish(job["id"], status, now)
        except RuntimeError as exc:
            if str(exc) != "worker_lease_lost":
                self._finish(job["id"], "failed", now, f"RuntimeError: {exc}")
        except Exception as exc:
            self._finish(job["id"], "failed", now, f"{type(exc).__name__}: {exc}")
        return job

    def run_once(self, now: datetime | None = None, max_jobs: int = 100) -> int:
        now = utc(now or datetime.now(timezone.utc))
        self.seed(now)
        count = 0
        while count < max_jobs and self.process_one(now) is not None:
            count += 1
        if self.remote_notifier is not None:
            try:
                RemoteDeliveryDispatcher(self.repository, self.remote_notifier).dispatch(now)
            except Exception as exc:
                self.repository.append_trace("remote_dispatch_failed",
                                             {"error_type": type(exc).__name__})
        return count

    def run_forever(self, poll_seconds: float = 5) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds_must_be_positive")
        while True:
            self.run_once()
            time.sleep(poll_seconds)
