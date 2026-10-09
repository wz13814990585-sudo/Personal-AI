"""ntfy's public transport only receives a generic message and a per-device topic."""

from datetime import datetime, timedelta, timezone

import pytest

from personal_ai_os.ntfy_notifications import (
    MESSAGE, TITLE, NtfyNotifier, notifier_from_environment,
)
from personal_ai_os.remote_access import DeviceAccess
from personal_ai_os.remote_delivery import RemoteDeliveryDispatcher, RemoteNotificationKnownFailure
from personal_ai_os.storage import Repository
from personal_ai_os.worker import Worker, set_task_reminder
from personal_ai_os.contracts import TaskCreate, TimeBlockCreate
from test_p2_remote import notice


SECRET = "ab" * 32
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakeConnection:
    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error
        self.requests = []
        self.closed = False

    def request(self, method, path, body, headers):
        self.requests.append((method, path, body, headers))
        if self.error:
            raise self.error

    def getresponse(self):
        return FakeResponse(self.status)

    def close(self):
        self.closed = True


def test_config_and_device_topics_are_secret_and_stable():
    assert notifier_from_environment({}) is None
    with pytest.raises(ValueError, match="invalid_ntfy_topic_secret"):
        notifier_from_environment({"NTFY_TOPIC_SECRET": "weak"})
    sender = notifier_from_environment({"NTFY_TOPIC_SECRET": SECRET})
    assert isinstance(sender, NtfyNotifier)
    first = sender.topic_for_device("a" * 32)
    assert first == NtfyNotifier(SECRET).topic_for_device("a" * 32)
    assert first != sender.topic_for_device("b" * 32)
    assert len(first) == 48 and first.startswith("paos_")
    assert SECRET not in first and "a" * 32 not in first
    with pytest.raises(ValueError, match="invalid_remote_device_id"):
        sender.topic_for_device("../bad")


@pytest.mark.parametrize("status,error,expected", [
    (200, None, None),
    (403, None, RemoteNotificationKnownFailure),
    (429, None, RemoteNotificationKnownFailure),
    (500, None, RuntimeError),
    (200, TimeoutError("timeout"), TimeoutError),
])
def test_http_result_and_generic_payload(status, error, expected):
    connection = FakeConnection(status, error)
    sender = NtfyNotifier(SECRET, connection_factory=lambda: connection)
    private = "PRIVATE TASK TITLE AND CONTENT"
    if expected:
        with pytest.raises(expected):
            sender.send("a" * 32, private, private, private)
    else:
        sender.send("a" * 32, private, private, private)
    assert connection.closed
    method, path, body, headers = connection.requests[0]
    assert method == "POST" and path == "/" + sender.topic_for_device("a" * 32)
    assert body.decode() == MESSAGE and headers["X-Title"] == TITLE
    assert private not in str(connection.requests)


def test_worker_quiet_hours_restart_and_unknown_are_not_resent(tmp_path):
    repo = Repository(tmp_path / "ntfy.sqlite3")
    repo.initialize()
    repo.set_setting("timezone", "Australia/Sydney")
    repo.set_setting("quiet_start", "22:00")
    repo.set_setting("quiet_end", "07:00")
    access = DeviceAccess(repo)
    device = access.create_device("Phone", now=NOW - timedelta(minutes=1))["device"]
    access.set_notifications(device["id"], device["version"], True, now=NOW)
    # 2026-10-09 23:30 Sydney is inside quiet hours; task begins 08:00 next day.
    start = datetime(2026, 10, 9, 21, tzinfo=timezone.utc)
    repo.create_time_block(TimeBlockCreate(
        kind="available", start_at=start, end_at=start + timedelta(minutes=30),
    ))
    task = repo.create_task(TaskCreate(
        title="Private task", start_at=start, end_at=start + timedelta(minutes=30),
    ))
    set_task_reminder(repo, task["id"], 510)
    connection = FakeConnection()
    sender = NtfyNotifier(SECRET, connection_factory=lambda: connection)
    Worker(repo, remote_notifier=sender).run_once(datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc))
    assert connection.requests == []
    reopened = Repository(repo.path)
    reopened.initialize()
    due = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)  # 07:00 next local day
    Worker(reopened, remote_notifier=sender).run_once(due)
    assert len(connection.requests) == 1
    assert RemoteDeliveryDispatcher(reopened, sender).list_deliveries()[0]["status"] == "delivered"
    Worker(reopened, remote_notifier=sender).run_once(due + timedelta(seconds=1))
    assert len(connection.requests) == 1

    # A timeout may have delivered; persistent status is unknown and no retry occurs.
    late = notice(reopened, at=due + timedelta(seconds=2))
    timeout_connection = FakeConnection(error=TimeoutError("timeout"))
    uncertain = NtfyNotifier(SECRET, connection_factory=lambda: timeout_connection)
    Worker(reopened, remote_notifier=uncertain).run_once(due + timedelta(seconds=3), max_jobs=0)
    rows = RemoteDeliveryDispatcher(reopened, uncertain).list_deliveries()
    assert any(row["notification_id"] == late and row["status"] == "unknown" for row in rows)
    Worker(reopened, remote_notifier=uncertain).run_once(due + timedelta(seconds=4), max_jobs=0)
    assert len(timeout_connection.requests) == 1
