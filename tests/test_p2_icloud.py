"""Deterministic EventKit adapter tests; no real calendar account is touched."""

from datetime import date

import pytest

from personal_ai_os.external import ExternalKnownFailure, ExternalUnknownResult
from personal_ai_os.services import PersonalAIService


class BridgeFake:
    def __init__(self):
        self.calls = []
        self.mode = "success"
        self.conflict = False
        self.fingerprint = "a" * 64
        self.event = {
            "event_id": "event-1", "calendar_id": "icloud-calendar",
            "title": "原事件", "start_at_utc": "2026-10-10T10:00:00+00:00",
            "end_at_utc": "2026-10-10T11:00:00+00:00",
            "description": "原说明", "location": "原地点", "reminder_minutes": 10,
        }

    def call(self, operation, **values):
        self.calls.append((operation, values))
        if self.mode == "denied":
            raise ExternalKnownFailure("calendar_permission_denied")
        if operation == "list_calendars":
            return {"items": [
                {"calendar_id": "icloud-calendar", "title": "个人", "source_id": "source-1",
                 "source_title": "iCloud", "writable": True},
                {"calendar_id": "icloud-work", "title": "工作", "source_id": "source-1",
                 "source_title": "iCloud", "writable": True},
                {"calendar_id": "local-calendar", "title": "本机", "source_id": "source-2",
                 "source_title": "On My Mac", "writable": True},
            ]}
        if operation == "list_events":
            return {"items": [dict(self.event)]}
        if operation == "get_event":
            if values["event_id"] != "event-1":
                raise ExternalKnownFailure("icloud_event_not_found")
            return {"event": dict(self.event), "fingerprint": self.fingerprint}
        if operation == "inspect_write":
            if self.mode == "change_on_inspect":
                self.fingerprint = "b" * 64
            conflict = [{"title": "其他会议", "start_at_utc": "2026-10-10T10:00:00Z"}]
            return {"current": dict(self.event) if values.get("event_id") else None,
                    "fingerprint": self.fingerprint if values.get("event_id") else None,
                    "conflicts": conflict if self.conflict else []}
        if operation == "write_event":
            if self.mode == "timeout":
                raise TimeoutError("eventkit_result_unknown")
            if self.mode == "unknown":
                raise ExternalUnknownResult("icloud_save_result_unknown")
            if self.conflict:
                raise ExternalKnownFailure("icloud_calendar_conflict")
            if values.get("event_id") and values["event"].get("expected_event_fingerprint") != self.fingerprint:
                raise ExternalKnownFailure("icloud_event_changed")
            return {"event_id": values.get("event_id") or "created-1",
                    "calendar_id": "icloud-calendar", "event": values["event"]}
        raise AssertionError(operation)


def selected(flow_system):
    repo, _, gateway, _, harness, service = flow_system
    bridge = BridgeFake()
    service.icloud_bridge = bridge
    selection = service.select_icloud_calendar("icloud-calendar")
    return repo, gateway, harness, service, bridge, selection


def allow(service, selection, operation):
    server = next(item for item in service.list_external_servers()
                  if item["id"] == selection["server_id"])
    if server["status"] != "active":
        server = service.set_external_server_status(server["id"], server["version"], True)
    return service.set_external_operation_enabled(server["id"], operation,
                                                  server["version"], True)


def test_selection_permissions_and_rebind(flow_system):
    repo, gateway, harness, service, bridge, selection = selected(flow_system)
    assert [item["calendar_id"] for item in service.available_icloud_calendars()] == [
        "icloud-calendar", "icloud-work"
    ]
    with pytest.raises(ValueError, match="icloud_calendar_not_available"):
        service.select_icloud_calendar("local-calendar")
    with pytest.raises(PermissionError, match="external_operation_not_allowed"):
        service.read_icloud_local_days(date(2026, 10, 10), date(2026, 10, 10))
    allow(service, selection, "list_events")
    assert service.read_icloud_local_days(date(2026, 10, 10), date(2026, 10, 10))["items"][0]["title"] == "原事件"
    reopened = PersonalAIService(repo, gateway, harness, icloud_bridge=bridge)
    assert reopened.get_icloud_calendar_selection() == selection
    assert next(item for item in reopened.list_external_servers()
                if item["id"] == selection["server_id"])["bound"]
    bridge.mode = "denied"
    with pytest.raises(ExternalKnownFailure, match="calendar_permission_denied"):
        reopened.read_icloud_local_days(date(2026, 10, 10), date(2026, 10, 10))


def test_create_approval_conflict_reminder_and_idempotency(flow_system):
    _, _, _, service, bridge, selection = selected(flow_system)
    allow(service, selection, "create_event")
    proposal = service.propose_icloud_create("学习", "2026-10-10T21:00", "2026-10-10T22:00",
                                              "章节一", "家", 15)
    assert proposal["status"] == "pending" and proposal["payload"]["reminder_minutes"] == 15
    assert proposal["payload"]["start_at_utc"] == "2026-10-10T10:00:00+00:00"
    assert not [call for call in bridge.calls if call[0] == "write_event"]
    bridge.conflict = True
    assert service.approve_external_write(proposal["id"], 0)["status"] == "failed"
    bridge.conflict = False
    second = service.propose_icloud_create("学习", "2026-10-10T21:00", "2026-10-10T22:00",
                                            "章节一", "家", 15)
    assert second["id"] != proposal["id"]
    assert service.approve_external_write(second["id"], 0)["result"]["event_id"] == "created-1"
    count = len([call for call in bridge.calls if call[0] == "write_event"])
    assert service.approve_external_write(second["id"], 0)["status"] == "succeeded"
    assert len([call for call in bridge.calls if call[0] == "write_event"]) == count


def test_update_baseline_and_unknown_never_resends(flow_system):
    _, _, _, service, bridge, selection = selected(flow_system)
    allow(service, selection, "update_event")
    proposal = service.propose_icloud_update("event-1", "改后", "2026-10-10T21:00",
                                              "2026-10-10T22:00", reminder_minutes=30)
    assert proposal["payload"]["expected_event_fingerprint"] == "a" * 64
    assert proposal["payload"]["selected_calendar_id"] == "icloud-calendar"
    bridge.fingerprint = "b" * 64
    assert service.approve_external_write(proposal["id"], 0)["status"] == "failed"
    bridge.fingerprint = "a" * 64
    second = service.propose_icloud_update("event-1", "改后", "2026-10-10T21:00",
                                            "2026-10-10T22:00", reminder_minutes=30)
    bridge.mode = "unknown"
    assert service.approve_external_write(second["id"], 0)["status"] == "unknown"
    assert service.propose_icloud_update("event-1", "改后", "2026-10-10T21:00",
                                          "2026-10-10T22:00", reminder_minutes=30)["id"] == second["id"]
    assert service.approve_external_write(second["id"], 0)["status"] == "unknown"
    assert len([call for call in bridge.calls if call[0] == "write_event"]) == 2


def test_switching_selected_calendar_rejects_old_update(flow_system):
    _, _, _, service, bridge, selection = selected(flow_system)
    allow(service, selection, "update_event")
    proposal = service.propose_icloud_update("event-1", "改后", "2026-10-10T21:00",
                                              "2026-10-10T22:00")
    service.select_icloud_calendar("icloud-work")
    result = service.approve_external_write(proposal["id"], 0)
    assert result["status"] == "failed"
    assert result["error_code"] == "icloud_calendar_not_selected"
    assert not [call for call in bridge.calls if call[0] == "write_event"]


def test_change_between_update_reads_rejects_stale_merge(flow_system):
    _, _, _, service, bridge, selection = selected(flow_system)
    allow(service, selection, "update_event")
    bridge.mode = "change_on_inspect"
    with pytest.raises(ExternalKnownFailure, match="icloud_event_changed"):
        service.propose_icloud_update("event-1", "改后", "2026-10-10T21:00",
                                      "2026-10-10T22:00")
    assert service.list_external_write_proposals() == []


def test_dst_gap_overlap_and_preview_conflict(flow_system):
    _, _, _, service, bridge, selection = selected(flow_system)
    allow(service, selection, "create_event")
    with pytest.raises(ValueError, match="nonexistent_local_calendar_time"):
        service.propose_icloud_create("gap", "2026-10-04T02:30", "2026-10-04T03:30")
    with pytest.raises(ValueError, match="ambiguous_local_calendar_time"):
        service.propose_icloud_create("overlap", "2026-04-05T02:30", "2026-04-05T03:30")
    assert service._calendar_local_to_utc("2026-10-04T03:30") == "2026-10-03T16:30:00+00:00"
    bridge.conflict = True
    with pytest.raises(ExternalKnownFailure, match="其他会议"):
        service.propose_icloud_create("conflict", "2026-10-10T21:00", "2026-10-10T22:00")
    assert service.list_external_write_proposals() == []


def test_local_day_read_range_includes_dst_fallback(flow_system):
    _, _, _, service, bridge, selection = selected(flow_system)
    allow(service, selection, "list_events")
    service.read_icloud_local_days(date(2026, 4, 1), date(2026, 5, 1))
    query = next(values for operation, values in reversed(bridge.calls)
                 if operation == "list_events")
    assert query["start_at_utc"] == "2026-03-31T13:00:00+00:00"
    assert query["end_at_utc"] == "2026-05-01T14:00:00+00:00"


@pytest.mark.parametrize("mode,status", [("timeout", "timeout"), ("unknown", "unknown")])
def test_uncertain_result_retains_one_attempt(flow_system, mode, status):
    _, _, _, service, bridge, selection = selected(flow_system)
    allow(service, selection, "create_event")
    proposal = service.propose_icloud_create("学习", "2026-10-10T21:00", "2026-10-10T22:00")
    bridge.mode = mode
    assert service.approve_external_write(proposal["id"], 0)["status"] == status
    assert service.approve_external_write(proposal["id"], 0)["status"] == status
    assert len([call for call in bridge.calls if call[0] == "write_event"]) == 1
