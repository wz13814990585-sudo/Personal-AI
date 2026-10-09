"""User-initiated application actions; UI never receives approval capabilities."""

from __future__ import annotations

import json
import os
import hashlib
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agent_registry import AgentRegistry
from .contracts import (
    CustomAgentCreate, CustomPlanningStep, DailyPlanAction, DailyPlanDraft, GoalCreate, HabitCheckin, HabitCreate, PlanDraft, RecurrenceRuleCreate,
    TaskCreate, TaskDraft, TimeBlockCreate,
)
from .conversation import recent_plan_history
from .custom_agent_runtime import CustomAgentRuntime, CustomModelRunner
from .custom_agents import CUSTOM_AGENT_READ_TOOLS, validate_custom_agent
from .custom_planning import execute_graph, validate_graph
from .daily_planning import (
    approve_daily_plan, daily_overview, edit_daily_plan, get_daily_plan,
    list_daily_plans, propose_daily_plan,
)
from .harness import Harness
from .orchestrator import required_worker_roles
from .recurrence import generate_instances
from .recurrence_suggestions import weekly_three_suggestions
from .proactive import (
    current_strategy, edit_suggestion, list_strategy_versions,
    list_suggestions, resolve_suggestion, rollback_strategy, scan_suggestions,
)
from .storage import Repository
from .tool_gateway import ToolGateway
from .export import export_json, export_tasks_csv
from .external import ExternalGateway
from .icloud_calendar import ICloudCalendarAdapter, MacEventKitBridge
from .gmail import GmailAdapter, GmailOAuth, MacKeychainStore
from .youtube_mcp import YouTubeMcpAdapter, OPERATIONS as YOUTUBE_OPERATIONS
from .remote_access import DeviceAccess
from .remote_delivery import RemoteDeliveryDispatcher, RemoteNotifier
from .ntfy_notifications import NtfyNotifier, TITLE as NTFY_TITLE, MESSAGE as NTFY_MESSAGE
from .usage import set_model_prices, usage_summary
from .worker import (
    create_daily_review, get_task_reminder, list_daily_reviews, list_notifications,
    mark_notification_read, save_daily_review_note, set_task_reminder,
)


class RemoteReadService:
    """Only session validation and read snapshots for the separately started view."""

    def __init__(self, repository: Repository):
        self.devices = DeviceAccess(repository)

    def open_remote_session(self, device_token: str) -> str:
        return self.devices.open_session(device_token)

    def close_remote_session(self, session_token: str) -> None:
        self.devices.close_session(session_token)

    def remote_snapshot(self, session_token: str) -> dict[str, Any]:
        return self.devices.snapshot(session_token)


class PersonalAIService:
    def __init__(
        self, repository: Repository, gateway: ToolGateway, harness: Harness,
        registry: AgentRegistry | None = None,
        custom_runner: CustomModelRunner | None = None,
        external_adapters: dict[str, object] | None = None,
        remote_notifier: RemoteNotifier | None = None,
        icloud_bridge: MacEventKitBridge | None = None,
        gmail_oauth: GmailOAuth | None = None,
        gmail_adapter: GmailAdapter | None = None,
    ):
        self.demo_mode = os.environ.get("PERSONAL_AI_DEMO") == "1"
        self.repository = repository
        self.gateway = gateway
        self.harness = harness
        self.registry = registry or gateway.registry
        self.custom_runtime = CustomAgentRuntime(
            repository, gateway, harness.config, custom_runner
        )
        self.external = ExternalGateway(repository, external_adapters)
        self.icloud_bridge = icloud_bridge or MacEventKitBridge(repository.path.parent / "eventkit-helper")
        self._bind_saved_icloud_calendar()
        self.gmail_account = os.environ.get("GMAIL_ACCOUNT", "").strip().lower()
        client_path = os.environ.get("GMAIL_OAUTH_CLIENT_PATH", "").strip()
        self.gmail_oauth = gmail_oauth or (
            GmailOAuth(Path(client_path), MacKeychainStore(repository.path.parent / "gmail-keychain"))
            if client_path else None
        )
        self.gmail_adapter = gmail_adapter or (GmailAdapter(self.gmail_account, self.gmail_oauth)
                                               if self.gmail_account and self.gmail_oauth else None)
        self._bind_saved_gmail()
        self._bind_saved_youtube()
        self.devices = DeviceAccess(repository)
        self.remote_notifier = remote_notifier

    @property
    def remote_notifications_available(self) -> bool:
        return self.remote_notifier is not None

    def create_remote_device(self, name: str, ttl_days: int = 30) -> dict[str, Any]:
        return self.devices.create_device(name, ttl_days=ttl_days)

    def list_remote_devices(self) -> list[dict[str, Any]]:
        return self.devices.list_devices()

    def remote_notification_subscription(self, device_id: str) -> dict[str, str] | None:
        """Give the local settings page a topic; never expose it in mobile views or exports."""
        if not isinstance(self.remote_notifier, NtfyNotifier):
            return None
        device = self.devices.get_device(device_id)
        if device["status"] != "active":
            return None
        return {"server": "ntfy.sh", "topic": self.remote_notifier.topic_for_device(device_id),
                "title": NTFY_TITLE, "message": NTFY_MESSAGE}

    def revoke_remote_device(self, device_id: str, version: int) -> dict[str, Any]:
        return self.devices.revoke_device(device_id, version)

    def set_remote_device_notifications(self, device_id: str, version: int,
                                        enabled: bool) -> dict[str, Any]:
        if enabled and self.remote_notifier is None:
            raise RuntimeError("remote_notification_channel_unavailable")
        return self.devices.set_notifications(device_id, version, enabled)

    def open_remote_session(self, device_token: str) -> str:
        return self.devices.open_session(device_token)

    def close_remote_session(self, session_token: str) -> None:
        self.devices.close_session(session_token)

    def authenticate_remote_session(self, session_token: str) -> str:
        return self.devices.authenticate_session(session_token)

    def remote_snapshot(self, session_token: str) -> dict[str, Any]:
        return self.devices.snapshot(session_token)

    def list_remote_deliveries(self) -> list[dict[str, Any]]:
        return RemoteDeliveryDispatcher(self.repository, self.remote_notifier).list_deliveries()

    def list_external_servers(self) -> list[dict[str, Any]]:
        return self.external.list_servers()

    def _bind_saved_youtube(self) -> None:
        server_id = self.repository.get_setting("youtube_mcp_server_id")
        if isinstance(server_id, str) and server_id not in self.external.adapters and any(
            item["id"] == server_id and item["kind"] == "mcp" and
            {op["operation"]: op["access"] for op in item["operations"]} == YOUTUBE_OPERATIONS
            for item in self.external.list_servers()
        ):
            self.external.adapters[server_id] = YouTubeMcpAdapter()

    def register_youtube_mcp(self) -> dict[str, Any]:
        """Register the fixed local server paused, with both reads disabled."""
        if self.demo_mode:
            raise PermissionError("demo_external_disabled")
        server_id = self.repository.get_setting("youtube_mcp_server_id")
        existing = next((item for item in self.external.list_servers() if item["id"] == server_id), None)
        if existing:
            self._bind_saved_youtube()
            return existing
        server = self.external.register_server("YouTube public metadata (local stdio)", "mcp", YOUTUBE_OPERATIONS)
        self.repository.set_setting("youtube_mcp_server_id", server["id"])
        self._bind_saved_youtube()
        return server

    def youtube_status(self) -> dict[str, Any]:
        server_id = self.repository.get_setting("youtube_mcp_server_id")
        server = next((item for item in self.external.list_servers() if item["id"] == server_id), None)
        enabled = {item["operation"] for item in server["operations"] if item["enabled"]} if server else set()
        return {"server_id": server_id if server else None,
                "api_key_configured": bool(os.environ.get("YOUTUBE_API_KEY", "").strip()),
                "ready": bool(server and server["status"] == "active" and server["bound"]
                              and enabled == set(YOUTUBE_OPERATIONS))}

    def _youtube_server_id(self) -> str:
        server_id = self.youtube_status()["server_id"]
        if not server_id:
            raise RuntimeError("youtube_mcp_not_registered")
        return server_id

    def search_youtube(self, query: str, max_results: int = 3,
                       *, run_id: str | None = None) -> dict[str, Any]:
        """Search and inspect public metadata; retain a bounded daily search budget."""
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120 or type(max_results) is not int or not 1 <= max_results <= 3:
            raise ValueError("youtube_invalid_arguments")
        server_id = self._youtube_server_id()
        # Check both operations before reserving quota or returning cached results.
        self.external._authorized(server_id, "search_videos", "read")
        self.external._authorized(server_id, "get_video_details", "read")
        key = hashlib.sha256(f"{query.strip()}:{max_results}".encode()).hexdigest()
        day = datetime.now(timezone.utc).date().isoformat()
        with self.repository.transaction() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key='youtube_search_state'").fetchone()
            state = json.loads(row["value_json"]) if row else {}
            if state.get("day") != day:
                state = {"day": day, "count": 0, "cache": {}}
            cached = state["cache"].get(key)
            if cached is None:
                if state["count"] >= 10:
                    raise RuntimeError("youtube_local_search_budget_exceeded")
                state["count"] += 1
                conn.execute("INSERT INTO settings(key,value_json) VALUES ('youtube_search_state',?) "
                             "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                             (json.dumps(state, ensure_ascii=False),))
        if cached is not None:
            if run_id:
                self.repository.append_trace("youtube_mcp_cache_hit", {"count": len(cached)},
                                             run_id=run_id, actor="system")
            return {"items": cached, "source": {"server_id": server_id, "cache": True}}
        matches = self.read_external(server_id, "search_videos", "public",
                                     {"query": query.strip(), "max_results": max_results})["items"]
        if run_id:
            self.repository.append_trace("youtube_mcp_read", {
                "server_id": server_id, "operation": "search_videos", "count": len(matches),
            }, run_id=run_id, actor="system")
        videos = []
        for item in matches:
            details = self.read_external(server_id, "get_video_details", "public",
                                         {"video_id": item["video_id"]})["items"]
            if run_id:
                self.repository.append_trace("youtube_mcp_read", {
                    "server_id": server_id, "operation": "get_video_details",
                    "video_id": item["video_id"], "count": len(details),
                }, run_id=run_id, actor="system")
            if details:
                videos.append(details[0])
        with self.repository.transaction() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key='youtube_search_state'").fetchone()
            state = json.loads(row["value_json"]) if row else {"day": day, "count": 1, "cache": {}}
            if state.get("day") == day:
                cache = state.setdefault("cache", {})
                if len(cache) >= 10:
                    cache.pop(next(iter(cache)))
                cache[key] = videos
                conn.execute("UPDATE settings SET value_json=? WHERE key='youtube_search_state'",
                             (json.dumps(state, ensure_ascii=False),))
        return {"items": videos, "source": {"server_id": server_id, "cache": False}}

    def get_youtube_video_details(self, video_id: str) -> dict[str, Any]:
        return self.read_external(self._youtube_server_id(), "get_video_details", "public",
                                  {"video_id": video_id})

    def _selected_youtube_resources(self, run_id: str, request_text: str) -> list[dict[str, Any]]:
        try:
            resources = self.search_youtube(request_text, run_id=run_id)["items"]
            if not resources:
                raise ValueError("youtube_no_public_results")
            self.repository.append_trace("youtube_resources_selected", {
                "video_ids": [item["video_id"] for item in resources],
                "count": len(resources), "source": "YouTube Data API v3",
            }, run_id=run_id, actor="user")
            return resources
        except Exception as exc:
            self.repository.update_run_status(run_id, "failed")
            self.repository.append_trace("youtube_resources_failed", {
                "error_code": str(exc) if str(exc) in {
                    "youtube_mcp_timeout", "youtube_mcp_invalid_response", "youtube_quota_or_permission",
                    "youtube_local_search_budget_exceeded", "youtube_no_public_results",
                    "youtube_api_key_missing", "youtube_mcp_not_registered",
                    "youtube_transport_error", "youtube_http_error", "youtube_mcp_call_failed",
                    "external_operation_not_allowed"} else type(exc).__name__,
            }, run_id=run_id, actor="system")
            raise

    def _bind_saved_gmail(self) -> None:
        server_id = self.repository.get_setting("gmail_server_id")
        if not isinstance(server_id, str) or self.gmail_adapter is None:
            return
        if any(item["id"] == server_id and item["kind"] == "mail"
               for item in self.external.list_servers()):
            self.external.adapters[server_id] = self.gmail_adapter

    def gmail_status(self) -> dict[str, Any]:
        server_id = self.repository.get_setting("gmail_server_id")
        server = next((item for item in self.external.list_servers()
                       if item["id"] == server_id and item["kind"] == "mail"), None)
        return {"configured": bool(self.gmail_account and self.gmail_oauth),
                "account": self.gmail_account or None,
                "connected": bool(server and server["bound"]),
                "server_id": server_id if server else None}

    def connect_gmail(self) -> dict[str, Any]:
        if self.demo_mode:
            raise PermissionError("demo_external_disabled")
        if not self.gmail_account or self.gmail_oauth is None or self.gmail_adapter is None:
            raise RuntimeError("gmail_oauth_not_configured")
        result = self.gmail_oauth.authorize(self.gmail_account)
        if result.get("account", "").casefold() != self.gmail_account:
            raise PermissionError("gmail_account_mismatch")
        server_id = self.repository.get_setting("gmail_server_id")
        server = next((item for item in self.external.list_servers()
                       if item["id"] == server_id and item["kind"] == "mail"), None)
        if server is None:
            server = self.external.register_server("Gmail", "mail")
            self.repository.set_setting("gmail_server_id", server["id"])
        self._bind_saved_gmail()
        return self.gmail_status()

    def _gmail_target(self) -> tuple[str, str]:
        status = self.gmail_status()
        if not status["connected"]:
            raise RuntimeError("gmail_not_connected")
        return status["server_id"], status["account"]

    def list_gmail_messages(self, page_size: int = 10,
                            page_token: str = "", folder: str = "INBOX") -> dict[str, Any]:
        server_id, account = self._gmail_target()
        return self.read_external(server_id, "list_messages", account,
                                  {"page_size": page_size, "page_token": page_token,
                                   "folder": folder})

    def get_gmail_message(self, message_id: str) -> dict[str, Any]:
        server_id, account = self._gmail_target()
        return self.read_external(server_id, "get_message", account,
                                  {"message_id": message_id})

    def propose_gmail_send(self, recipient: str, subject: str,
                           body: str) -> dict[str, Any]:
        server_id, account = self._gmail_target()
        return self.propose_external_write(server_id, "send_message", account,
                                           recipient, {"subject": subject, "body": body})

    def _bind_saved_icloud_calendar(self) -> None:
        selection = self.get_icloud_calendar_selection()
        if not selection:
            return
        server = next((item for item in self.external.list_servers()
                       if item["id"] == selection.get("server_id") and item["kind"] == "calendar"), None)
        if server and isinstance(selection.get("calendar_id"), str):
            self.external.adapters[server["id"]] = ICloudCalendarAdapter(
                self.icloud_bridge, selection["calendar_id"]
            )

    def get_icloud_calendar_selection(self) -> dict[str, Any] | None:
        value = self.repository.get_setting("icloud_calendar_selection")
        return value if isinstance(value, dict) else None

    def available_icloud_calendars(self) -> list[dict[str, Any]]:
        if self.demo_mode:
            raise PermissionError("demo_external_disabled")
        items = self.icloud_bridge.call("list_calendars").get("items")
        if not isinstance(items, list):
            raise RuntimeError("invalid_icloud_calendar_list")
        return [item for item in items if isinstance(item, dict) and item.get("writable")
                and "icloud" in str(item.get("source_title", "")).lower()]

    def select_icloud_calendar(self, calendar_id: str) -> dict[str, Any]:
        chosen = next((item for item in self.available_icloud_calendars()
                       if item.get("calendar_id") == calendar_id), None)
        if chosen is None:
            raise ValueError("icloud_calendar_not_available")
        previous = self.get_icloud_calendar_selection()
        server = None
        if previous:
            server = next((item for item in self.external.list_servers()
                           if item["id"] == previous.get("server_id")
                           and item["kind"] == "calendar"), None)
        if server is None:
            server = self.external.register_server("iCloud Calendar", "calendar")
        selection = {
            "server_id": server["id"], "calendar_id": chosen["calendar_id"],
            "title": chosen["title"], "source_id": chosen["source_id"],
            "source_title": chosen["source_title"],
        }
        self.repository.set_setting("icloud_calendar_selection", selection)
        self._bind_saved_icloud_calendar()
        return selection

    def _icloud_target(self) -> tuple[str, str]:
        selection = self.get_icloud_calendar_selection()
        if not selection:
            raise RuntimeError("icloud_calendar_not_selected")
        return selection["server_id"], selection["calendar_id"]

    def read_icloud_events(self, start_at_utc: str, end_at_utc: str) -> dict[str, Any]:
        server_id, _ = self._icloud_target()
        return self.read_external(server_id, "list_events", "icloud",
                                  {"start_at_utc": start_at_utc, "end_at_utc": end_at_utc})

    def read_icloud_local_days(self, first_day: date, last_day: date) -> dict[str, Any]:
        if last_day < first_day or (last_day - first_day).days > 30:
            raise ValueError("invalid_calendar_range")
        start = self._calendar_local_to_utc(first_day.isoformat() + "T00:00:00")
        end = self._calendar_local_to_utc((last_day + timedelta(days=1)).isoformat() + "T00:00:00")
        return self.read_icloud_events(start, end)

    def _calendar_local_to_utc(self, value: str) -> str:
        try:
            local = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid_local_calendar_time") from exc
        if local.tzinfo is not None:
            raise ValueError("local_calendar_time_must_be_naive")
        zone = ZoneInfo(self.timezone)
        valid = []
        for fold in (0, 1):
            candidate = local.replace(tzinfo=zone, fold=fold)
            instant = candidate.astimezone(timezone.utc)
            if instant.astimezone(zone).replace(tzinfo=None) == local and instant not in valid:
                valid.append(instant)
        if not valid:
            raise ValueError("nonexistent_local_calendar_time")
        if len(valid) > 1:
            raise ValueError("ambiguous_local_calendar_time")
        return valid[0].isoformat()

    def _icloud_event_payload(self, title: str, start_local: str, end_local: str,
                              description: str, location: str,
                              reminder_minutes: int | None) -> dict[str, Any]:
        start, end = self._calendar_local_to_utc(start_local), self._calendar_local_to_utc(end_local)
        if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
            raise ValueError("invalid_calendar_time")
        return {"title": title, "start_at_utc": start, "end_at_utc": end,
                "description": description, "location": location,
                "reminder_minutes": reminder_minutes}

    def propose_icloud_create(self, title: str, start_local: str, end_local: str,
                              description: str = "", location: str = "",
                              reminder_minutes: int | None = None) -> dict[str, Any]:
        server_id, calendar_id = self._icloud_target()
        payload = self._icloud_event_payload(title, start_local, end_local,
                                              description, location, reminder_minutes)
        return self.propose_external_write(server_id, "create_event", "icloud", calendar_id,
                                           payload)

    def propose_icloud_update(self, event_id: str, title: str, start_local: str,
                              end_local: str, description: str = "", location: str = "",
                              reminder_minutes: int | None = None) -> dict[str, Any]:
        server_id, _ = self._icloud_target()
        payload = self._icloud_event_payload(title, start_local, end_local,
                                              description, location, reminder_minutes)
        return self.propose_external_write(server_id, "update_event", "icloud", event_id,
                                           payload)

    def register_external_server(self, name: str, kind: str,
                                 operations: dict[str, str] | None = None) -> dict[str, Any]:
        return self.external.register_server(name, kind, operations)

    def set_external_server_status(self, server_id: str, version: int,
                                   active: bool) -> dict[str, Any]:
        return self.external.set_server_status(server_id, version, active)

    def set_external_operation_enabled(self, server_id: str, operation: str,
                                       version: int, enabled: bool) -> dict[str, Any]:
        return self.external.set_operation_enabled(server_id, operation, version, enabled)

    def read_external(self, server_id: str, operation: str, account_id: str,
                      query: dict[str, Any]) -> dict[str, Any]:
        if self.demo_mode:
            raise PermissionError("demo_external_disabled")
        return self.external.read(server_id, operation, account_id, query)

    def propose_external_write(self, server_id: str, operation: str, account_id: str,
                               target: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.demo_mode:
            raise PermissionError("demo_external_disabled")
        return self.external.propose_write(server_id, operation, account_id, target, payload)

    def list_external_write_proposals(self) -> list[dict[str, Any]]:
        return self.external.list_proposals()

    def approve_external_write(self, proposal_id: str, revision: int) -> dict[str, Any]:
        if self.demo_mode:
            raise PermissionError("demo_external_disabled")
        return self.external.approve_write(proposal_id, revision)

    def reject_external_write(self, proposal_id: str, revision: int) -> dict[str, Any]:
        return self.external.reject_write(proposal_id, revision)

    def list_external_audit(self) -> list[dict[str, Any]]:
        return self.external.list_audit()

    def recover_external_attempts(self, now: datetime | None = None) -> int:
        return self.external.recover_expired_attempts(now)

    @property
    def timezone(self) -> str:
        return self.harness.config.timezone

    @property
    def model_ready(self) -> bool:
        return bool(self.harness.config.deepseek_api_key)

    def list_runs(self, *, conversation_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.repository.list_runs()
        return [row for row in rows if row["conversation_id"] == conversation_id] if conversation_id else rows

    def plan_history(self, conversation_id: str) -> list[dict[str, str]]:
        return recent_plan_history(self.repository.list_runs(), conversation_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self.repository.get_run(run_id)

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        return self.repository.list_run_steps(run_id)

    def list_trace(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_trace(run_id)

    def list_model_usage(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_model_usage(run_id)

    def usage_summary(self, run_id: str | None = None) -> dict[str, Any]:
        return usage_summary(self.repository, run_id=run_id)

    def model_prices(self) -> dict[str, str]:
        return self.repository.get_setting("model_prices", {}).get(self.harness.config.deepseek_model_id, {})

    def set_model_prices(self, input_per_million: str, output_per_million: str) -> None:
        set_model_prices(
            self.repository, self.harness.config.deepseek_model_id,
            input_per_million, output_per_million,
        )
        self.repository.append_trace("model_prices_saved", {
            "model_id": self.harness.config.deepseek_model_id
        }, actor="user")

    def export_data(self, format: str = "json") -> bytes:
        if format == "json":
            return export_json(self.repository)
        if format == "tasks_csv":
            return export_tasks_csv(self.repository)
        raise ValueError("unsupported_export_format")

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_tasks(status)

    def get_task_reminder(self, task_id: str) -> dict[str, Any] | None:
        return get_task_reminder(self.repository, task_id)

    def set_task_reminder(self, task_id: str, lead_minutes: int, enabled: bool = True) -> dict[str, Any]:
        result = set_task_reminder(self.repository, task_id, lead_minutes, enabled)
        self.repository.append_trace("task_reminder_saved", {"task_id": task_id}, actor="user")
        return result

    def list_notifications(self) -> list[dict[str, Any]]:
        return list_notifications(self.repository)

    def mark_notification_read(self, notification_id: str) -> dict[str, Any]:
        return mark_notification_read(self.repository, notification_id)

    def notification_preferences(self) -> dict[str, Any]:
        return {
            "quiet_start": self.repository.get_setting("quiet_start", "22:00"),
            "quiet_end": self.repository.get_setting("quiet_end", "07:00"),
            "system_notifications_enabled": self.repository.get_setting("system_notifications_enabled", False),
        }

    def set_notification_preferences(self, quiet_start: str, quiet_end: str,
                                     system_enabled: bool) -> dict[str, Any]:
        if self.demo_mode and system_enabled:
            raise PermissionError("demo_system_notification_disabled")
        from datetime import time
        for value in (quiet_start, quiet_end):
            try:
                if time.fromisoformat(value).strftime("%H:%M") != value:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("invalid_quiet_time") from exc
        with self.repository.transaction() as connection:
            import json
            for key, value in (("quiet_start", quiet_start), ("quiet_end", quiet_end),
                               ("system_notifications_enabled", bool(system_enabled))):
                connection.execute(
                    "INSERT INTO settings(key,value_json) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                    (key, json.dumps(value)),
                )
        self.repository.append_trace("notification_preferences_saved", {}, actor="user")
        return self.notification_preferences()

    def list_daily_reviews(self) -> list[dict[str, Any]]:
        return list_daily_reviews(self.repository)

    def list_proactive_suggestions(self, status: str | None = None) -> list[dict[str, Any]]:
        return list_suggestions(self.repository, status)

    def scan_proactive_suggestions(self) -> list[dict[str, Any]]:
        return scan_suggestions(self.repository)

    def edit_proactive_suggestion(
        self, suggestion_id: str, revision: int, title: str,
        explanation: str, proposed_threshold: int,
    ) -> dict[str, Any]:
        return edit_suggestion(
            self.repository, suggestion_id, revision, title, explanation, proposed_threshold,
        )

    def resolve_proactive_suggestion(
        self, suggestion_id: str, revision: int, accept: bool,
        *, as_of: datetime | None = None,
    ) -> dict[str, Any]:
        return resolve_suggestion(self.repository, suggestion_id, revision, accept, as_of=as_of)

    def current_suggestion_strategy(self) -> dict[str, Any]:
        return current_strategy(self.repository)

    def list_suggestion_strategy_versions(self) -> list[dict[str, Any]]:
        return list_strategy_versions(self.repository)

    def rollback_suggestion_strategy(
        self, target_version: int, expected_active_version: int,
    ) -> dict[str, Any]:
        return rollback_strategy(self.repository, target_version, expected_active_version)

    def create_daily_review(self, local_date: date | None = None) -> dict[str, Any]:
        day = local_date or datetime.now(ZoneInfo(self.timezone)).date()
        result = create_daily_review(self.repository, day, self.timezone, datetime.now().astimezone())
        self.repository.append_trace("daily_review_opened", {"review_id": result["id"]}, actor="user")
        scan_suggestions(self.repository)
        return result

    def save_daily_review_note(self, review_id: str, revision: int, note: str) -> dict[str, Any]:
        result = save_daily_review_note(self.repository, review_id, revision, note)
        self.repository.append_trace("daily_review_edited", {"review_id": review_id}, actor="user")
        scan_suggestions(self.repository)
        return result

    def today_tasks(self) -> list[dict[str, Any]]:
        today = datetime.now(ZoneInfo(self.harness.config.timezone)).date()
        return [
            task for task in self.repository.list_tasks()
            if task["start_at_utc"] and
            datetime.fromisoformat(task["start_at_utc"]).astimezone(
                ZoneInfo(self.harness.config.timezone)
            ).date() == today
        ]

    def daily_overview(self, local_date: date | None = None) -> dict[str, Any]:
        day = local_date or datetime.now(ZoneInfo(self.timezone)).date()
        return daily_overview(self.repository, day, self.timezone)

    def list_daily_plans(self) -> list[DailyPlanDraft]:
        return list_daily_plans(self.repository)

    def get_daily_plan(self, plan_id: str) -> DailyPlanDraft:
        return get_daily_plan(self.repository, plan_id)

    def propose_daily_plan(
        self, local_date: date | None = None, *, trigger_key: str | None = None,
        as_of: datetime | None = None,
    ) -> DailyPlanDraft:
        day = local_date or datetime.now(ZoneInfo(self.timezone)).date()
        draft = propose_daily_plan(
            self.repository, day, self.timezone, trigger_key=trigger_key, as_of=as_of
        )
        self.repository.append_trace(
            "daily_plan_proposed", {"plan_id": draft.id, "revision": draft.revision,
                                    "local_date": day.isoformat()}, actor="user"
        )
        return draft

    def edit_daily_plan(
        self, plan_id: str, revision: int,
        actions: list[DailyPlanAction | dict[str, Any]],
    ) -> DailyPlanDraft:
        draft = edit_daily_plan(self.repository, plan_id, revision, actions)
        self.repository.append_trace(
            "daily_plan_edited", {"plan_id": plan_id, "revision": draft.revision}, actor="user"
        )
        return draft

    def approve_daily_plan(self, plan_id: str, revision: int) -> list[dict[str, Any]]:
        return approve_daily_plan(self.repository, plan_id, revision)

    def create_task(self, data: TaskCreate) -> dict[str, Any]:
        task = self.repository.create_task(data)
        self.repository.append_trace("task_created", {"task_id": task["id"]}, actor="user")
        return task

    def update_task(self, task_id: str, data: TaskCreate,
                    expected_version: int | None = None) -> dict[str, Any]:
        task = self.repository.update_task(task_id, data, expected_version)
        self.repository.append_trace("task_updated", {"task_id": task_id}, actor="user")
        return task

    def delete_task(self, task_id: str) -> None:
        self.repository.delete_task(task_id)
        self.repository.append_trace("task_deleted", {"task_id": task_id}, actor="user")

    def list_recurrence_rules(self) -> list[dict[str, Any]]:
        return self.repository.list_recurrence_rules()

    def list_recurrence_suggestions(self, run_id: str) -> list[dict[str, Any]]:
        run = self.repository.get_run(run_id)
        if run["kind"] != "planning" or not run["draft_json"]:
            return []
        snapshot = json.loads(run["config_snapshot_json"])
        suggestions = weekly_three_suggestions(
            run["request_text"], self.get_plan(run_id),
            snapshot.get("timezone") or self.timezone,
        )
        confirmed = {row["id"] for row in self.repository.list_recurrence_rules()}
        return [{**item, "confirmed": item["rule_id"] in confirmed}
                for item in suggestions]

    def confirm_recurrence_suggestion(
        self, run_id: str, revision: int, item_id: str,
        weekdays: list[int], start_date: date,
    ) -> dict[str, Any]:
        result = self.repository.confirm_recurrence_suggestion(
            run_id, revision, item_id, weekdays, start_date
        )
        self.repository.append_trace(
            "recurrence_suggestion_confirmed",
            {"run_id": run_id, "item_id": item_id, "rule_id": result["id"]},
            run_id=run_id, actor="user",
        )
        return result

    def list_recurrence_instances(self, rule_id: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_recurrence_instances(rule_id)

    def create_recurrence_rule(self, data: RecurrenceRuleCreate) -> dict[str, Any]:
        rule = self.repository.create_recurrence_rule(data)
        self.repository.append_trace("recurrence_rule_confirmed", {"rule_id": rule["id"]}, actor="user")
        return rule

    def update_recurrence_rule(self, rule_id: str, data: RecurrenceRuleCreate) -> dict[str, Any]:
        rule = self.repository.update_recurrence_rule(rule_id, data)
        self.repository.append_trace("recurrence_rule_updated", {"rule_id": rule_id}, actor="user")
        return rule

    def set_recurrence_rule_status(self, rule_id: str, status: str) -> dict[str, Any]:
        rule = self.repository.set_recurrence_rule_status(rule_id, status)
        self.repository.append_trace(
            "recurrence_rule_status", {"rule_id": rule_id, "status": status}, actor="user"
        )
        return rule

    def generate_recurrence_instances(
        self, rule_id: str, start_date: date | None = None, end_date: date | None = None,
        *, as_of_local_date: date | None = None,
    ) -> list[dict[str, Any]]:
        rule = self.repository.get_recurrence_rule(rule_id)
        today = as_of_local_date or datetime.now(ZoneInfo(rule["timezone"])).date()
        first = start_date or today
        last = end_date or first + timedelta(days=6)
        created = generate_instances(
            self.repository, rule_id, first, last, as_of_local_date=today
        )
        self.repository.append_trace(
            "recurrence_scan", {"rule_id": rule_id, "created": len(created),
                                "from": first.isoformat(), "to": last.isoformat()}, actor="user"
        )
        return created

    def list_habits(self) -> list[dict[str, Any]]:
        return self.repository.list_habits()

    def create_habit(self, data: HabitCreate) -> dict[str, Any]:
        habit = self.repository.create_habit(data)
        self.repository.append_trace("habit_created", {"habit_id": habit["id"]}, actor="user")
        return habit

    def update_habit(self, habit_id: str, data: HabitCreate) -> dict[str, Any]:
        habit = self.repository.update_habit(habit_id, data)
        self.repository.append_trace("habit_updated", {"habit_id": habit_id}, actor="user")
        return habit

    def set_habit_status(self, habit_id: str, status: str) -> dict[str, Any]:
        habit = self.repository.set_habit_status(habit_id, status)
        self.repository.append_trace(
            "habit_status", {"habit_id": habit_id, "status": status}, actor="user"
        )
        return habit

    def list_habit_checkins(self, habit_id: str) -> list[dict[str, Any]]:
        return self.repository.list_habit_checkins(habit_id)

    def checkin_habit(self, habit_id: str, data: HabitCheckin) -> dict[str, Any]:
        checkin = self.repository.checkin_habit(habit_id, data)
        self.repository.append_trace(
            "habit_checkin_saved", {"habit_id": habit_id,
                                      "local_date": data.local_date.isoformat(),
                                      "completed": data.completed}, actor="user"
        )
        return checkin

    def set_task_status(self, task_id: str, status: str) -> dict[str, Any]:
        task = self.repository.set_task_status(task_id, status)
        self.repository.append_trace(
            "task_status_changed", {"task_id": task_id, "status": status}, actor="user"
        )
        if status == "completed":
            self._propose_replan_for_tasks([task])
        return task

    def _propose_replan_for_tasks(self, tasks: list[dict[str, Any]],
                                  extra_dates: set[date] | None = None) -> None:
        zone = ZoneInfo(self.timezone)
        today = datetime.now(zone).date()
        dates = {
            max(today, datetime.fromisoformat(item["start_at_utc"]).astimezone(zone).date())
            for item in tasks if item["start_at_utc"]
        }
        dates.update(extra_dates or set())
        for local_date in sorted(dates):
            self.propose_daily_plan(local_date)

    def _block_dates_for_unscheduled(self, *blocks: dict[str, Any] | None) -> set[date]:
        if not any(item["status"] != "completed" and not item["start_at_utc"]
                   for item in self.repository.list_tasks()):
            return set()
        zone = ZoneInfo(self.timezone)
        now = datetime.now(zone)
        return {
            max(now.date(), datetime.fromisoformat(block["start_at_utc"]).astimezone(zone).date())
            for block in blocks if block and block["kind"] == "available"
            and datetime.fromisoformat(block["end_at_utc"]) > now
        }

    def _tasks_affected_by_block(self, *blocks: dict[str, Any] | None) -> list[dict[str, Any]]:
        intervals = [(datetime.fromisoformat(block["start_at_utc"]),
                      datetime.fromisoformat(block["end_at_utc"]))
                     for block in blocks if block and block["kind"] == "available"]
        return [task for task in self.repository.list_tasks()
                if task["status"] != "completed" and task["start_at_utc"]
                and any(datetime.fromisoformat(task["start_at_utc"]) < end
                        and datetime.fromisoformat(task["end_at_utc"]) > start
                        for start, end in intervals)]

    def list_time_blocks(self) -> list[dict[str, Any]]:
        return self.repository.list_time_blocks()

    def create_time_block(self, data: TimeBlockCreate) -> dict[str, Any]:
        block = self.repository.create_time_block(data)
        self.repository.append_trace("time_block_created", {"block_id": block["id"]}, actor="user")
        self._propose_replan_for_tasks([], self._block_dates_for_unscheduled(block))
        return block

    def update_time_block(self, block_id: str, data: TimeBlockCreate) -> dict[str, Any]:
        previous = self.repository.get_time_block(block_id)
        block = self.repository.update_time_block(block_id, data, allow_pending_replan=True)
        self.repository.append_trace("time_block_updated", {"block_id": block_id}, actor="user")
        self._propose_replan_for_tasks(
            self._tasks_affected_by_block(previous, block),
            self._block_dates_for_unscheduled(previous, block),
        )
        return block

    def delete_time_block(self, block_id: str) -> None:
        previous = self.repository.get_time_block(block_id)
        self.repository.delete_time_block(block_id, allow_pending_replan=True)
        self.repository.append_trace("time_block_deleted", {"block_id": block_id}, actor="user")
        self._propose_replan_for_tasks(
            self._tasks_affected_by_block(previous),
            self._block_dates_for_unscheduled(previous),
        )

    def list_goals(self) -> list[dict[str, Any]]:
        return self.repository.list_goals()

    def create_goal(self, data: GoalCreate) -> dict[str, Any]:
        goal = self.repository.create_goal(data)
        self.repository.append_trace("goal_created", {"goal_id": goal["id"]}, actor="user")
        return goal

    def update_goal(self, goal_id: str, data: GoalCreate, status: str = "active") -> dict[str, Any]:
        goal = self.repository.update_goal(goal_id, data, status)
        self.repository.append_trace("goal_updated", {"goal_id": goal_id}, actor="user")
        return goal

    def delete_goal(self, goal_id: str) -> None:
        self.repository.delete_goal(goal_id)
        self.repository.append_trace("goal_deleted", {"goal_id": goal_id}, actor="user")

    def list_roles(self) -> list[dict[str, Any]]:
        return self.registry.list_roles()

    def update_role(self, role: str, instructions: str, tool_subset: set[str]) -> dict[str, Any]:
        updated = self.registry.update_role_instruction(role, instructions, tool_subset)
        self.repository.append_trace(
            "agent_config_updated", {"role": role, "version": updated["version"]}, actor="user"
        )
        return updated

    def custom_agent_tool_maximum(self) -> list[str]:
        return sorted(CUSTOM_AGENT_READ_TOOLS)

    def list_custom_agents(self) -> list[dict[str, Any]]:
        agents = self.repository.list_custom_agents()
        for agent in agents:
            if not set(agent["tool_subset"]) <= CUSTOM_AGENT_READ_TOOLS:
                raise PermissionError("stored_custom_agent_permissions_exceed_maximum")
        return agents

    def generate_custom_agent_proposal(self, goal: str) -> dict[str, Any]:
        return self.custom_runtime.propose(goal)

    def list_custom_agent_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.custom_runtime.store.list_proposals(status)

    def accept_custom_agent_proposal(
        self, proposal_id: str, revision: int, data: CustomAgentCreate,
    ) -> dict[str, Any]:
        return self.custom_runtime.store.accept_proposal(
            proposal_id, revision, validate_custom_agent(data)
        )

    def reject_custom_agent_proposal(self, proposal_id: str, revision: int) -> dict[str, Any]:
        return self.custom_runtime.store.reject_proposal(proposal_id, revision)

    def run_custom_agent(self, agent_id: str, request_text: str) -> dict[str, Any]:
        return self.custom_runtime.advise(agent_id, request_text)

    def list_custom_agent_runs(self) -> list[dict[str, Any]]:
        return self.custom_runtime.store.list_runs()

    def get_custom_agent_run(self, run_id: str) -> dict[str, Any]:
        return self.custom_runtime.store.get_run(run_id)

    def list_custom_agent_trace(self, run_id: str) -> list[dict[str, Any]]:
        return self.custom_runtime.store.list_events(run_id)

    def list_custom_agent_usage(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return self.custom_runtime.store.list_usage(run_id)

    def create_custom_agent(self, data: CustomAgentCreate) -> dict[str, Any]:
        agent = self.repository.create_custom_agent(validate_custom_agent(data))
        self.repository.append_trace(
            "custom_agent_created", {"agent_id": agent["id"], "version": agent["version"]},
            actor="user",
        )
        return agent

    def update_custom_agent(
        self, agent_id: str, expected_version: int, data: CustomAgentCreate,
    ) -> dict[str, Any]:
        agent = self.repository.update_custom_agent(
            agent_id, expected_version, validate_custom_agent(data)
        )
        self.repository.append_trace(
            "custom_agent_updated", {"agent_id": agent_id, "version": agent["version"]},
            actor="user",
        )
        return agent

    def set_custom_agent_status(
        self, agent_id: str, expected_version: int, status: str,
    ) -> dict[str, Any]:
        agent = self.repository.get_custom_agent(agent_id)
        if not set(agent["tool_subset"]) <= CUSTOM_AGENT_READ_TOOLS:
            raise PermissionError("stored_custom_agent_permissions_exceed_maximum")
        updated = self.repository.set_custom_agent_status(agent_id, expected_version, status)
        self.repository.append_trace(
            "custom_agent_status_changed",
            {"agent_id": agent_id, "version": updated["version"], "status": status},
            actor="user",
        )
        return updated

    def list_memory_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_memory_proposals(status)

    def list_memories(self) -> list[dict[str, Any]]:
        return self.repository.list_approved_memories()

    def list_feedback(self) -> list[dict[str, Any]]:
        return self.repository.list_feedback()

    def list_settings(self) -> dict[str, Any]:
        return self.repository.list_settings()

    def set_preference(self, key: str, value: Any) -> None:
        if key not in {"sleep_start", "sleep_end", "personal_goal", "study_preference"}:
            raise ValueError("unsupported_preference_key")
        self.repository.set_setting(key, value)
        self.repository.append_trace("setting_changed", {"key": key}, actor="user")

    def set_timezone(self, timezone_name: str) -> None:
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid_timezone") from exc
        self.repository.set_setting("timezone", timezone_name)
        self.harness.config = replace(self.harness.config, timezone=timezone_name)
        self.gateway.timezone_name = timezone_name
        self.repository.append_trace("setting_changed", {"key": "timezone"}, actor="user")

    def start_plan(
        self, request_text: str, *, conversation_id: str | None = None,
        idempotency_key: str | None = None,
        custom_steps: list[CustomPlanningStep | dict[str, Any]] | None = None,
        use_youtube: bool = False,
        youtube_query: str | None = None,
    ) -> PlanDraft:
        if type(use_youtube) is not bool or (use_youtube and "learning" not in required_worker_roles(request_text)):
            raise ValueError("youtube_requires_learning_path")
        if use_youtube and (not isinstance(youtube_query, str) or
                            not 1 <= len(youtube_query.strip()) <= 120):
            raise ValueError("youtube_search_query_required")
        layers = (validate_graph(self.repository, custom_steps,
                                 self.harness.config.max_custom_agent_steps)
                  if custom_steps else [])
        run = self.repository.create_run(
            request_text, conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            config_snapshot={"requested_youtube": use_youtube,
                             **({"youtube_query": youtube_query.strip()} if use_youtube else {}),
                             "requested_custom_steps": [
                step.model_dump(mode="json") for layer in layers for step in layer
            ]},
        )
        resources = self._selected_youtube_resources(run["id"], youtube_query.strip()) if use_youtube else None
        context = (execute_graph(
            self.repository, self.custom_runtime, run["id"], run["request_text"],
            layers, self.harness.config.run_timeout_seconds,
        ) if layers else None)
        return self.harness.execute_plan(run["id"], custom_context=context,
                                         youtube_resources=resources)

    def retry_run(self, failed_run_id: str) -> PlanDraft | list[dict[str, Any]]:
        original_steps = [step for step in self.repository.list_run_steps(failed_run_id)
                          if step["step_id"].startswith("custom_")]
        selected = [CustomPlanningStep(
            step_id=step["step_id"], agent_id=step["agent"],
            depends_on=json.loads(step["depends_on_json"]),
        ) for step in original_steps]
        if not selected:
            source = self.repository.get_run(failed_run_id)
            selected = [CustomPlanningStep.model_validate(item) for item in
                        json.loads(source["config_snapshot_json"]).get("requested_custom_steps", [])]
        layers = (validate_graph(self.repository, selected,
                                 self.harness.config.max_custom_agent_steps)
                  if selected else [])
        run = self.repository.create_retry_run(failed_run_id)
        source_config = json.loads(self.repository.get_run(failed_run_id)["config_snapshot_json"])
        requested_youtube = source_config.get("requested_youtube", False)
        if requested_youtube:
            self.repository.set_run_config_snapshot(run["id"], {"requested_youtube": True,
                                                                 "youtube_query": source_config["youtube_query"]})
        self.repository.append_trace(
            "retry_started", {"source_run_id": failed_run_id}, run_id=run["id"], actor="user"
        )
        if run["kind"] == "planning":
            resources = self._selected_youtube_resources(run["id"], source_config["youtube_query"]) if requested_youtube else None
            context = (execute_graph(
                self.repository, self.custom_runtime, run["id"], run["request_text"],
                layers, self.harness.config.run_timeout_seconds,
            ) if layers else None)
            return self.harness.execute_plan(run["id"], custom_context=context,
                                             youtube_resources=resources)
        return self.harness.execute_feedback(run["id"])

    def get_plan(self, run_id: str) -> PlanDraft:
        run = self.repository.get_run(run_id)
        if run["kind"] != "planning" or not run["draft_json"]:
            raise ValueError("plan_draft_unavailable")
        return PlanDraft.model_validate_json(run["draft_json"])

    def edit_plan(
        self, run_id: str, revision: int,
        tasks: list[TaskDraft | dict[str, Any]],
    ) -> PlanDraft:
        checked = [
            TaskDraft.model_validate(item.model_dump() if isinstance(item, TaskDraft) else item)
            for item in tasks
        ]
        draft = self.repository.edit_plan_draft(
            run_id, revision, checked, self.harness.config.timezone
        )
        self.repository.append_trace(
            "plan_edited", {"revision": draft.revision, "task_count": len(checked)},
            run_id=run_id, actor="user",
        )
        return draft

    def approve_plan(self, run_id: str, revision: int) -> list[dict[str, Any]]:
        approval = self.gateway.issue_approval(run_id, revision)
        try:
            return self.harness.commit_plan(run_id, revision, approval)
        except Exception as exc:
            self.repository.append_trace(
                "plan_commit_rejected", {"error_type": type(exc).__name__},
                run_id=run_id, actor="user",
            )
            raise

    def reject_plan(self, run_id: str, revision: int) -> None:
        self.repository.reject_plan(run_id, revision)
        self.repository.append_trace(
            "plan_rejected", {"revision": revision}, run_id=run_id, actor="user"
        )

    def complete_task(self, task_id: str) -> dict[str, Any]:
        task = self.repository.set_task_status(task_id, "completed")
        self.repository.append_trace(
            "task_completed", {"task_id": task_id}, actor="user"
        )
        self._propose_replan_for_tasks([task])
        return task

    def submit_feedback(self, task_id: str, body: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        feedback, run = self.repository.create_feedback_run(task_id, body)
        self.repository.append_trace(
            "feedback_submitted", {"feedback_id": feedback["id"], "task_id": task_id},
            run_id=run["id"], actor="user",
        )
        proposals = self.harness.execute_feedback(run["id"])
        return feedback, proposals

    def edit_memory_proposal(self, proposal_id: str, value: dict[str, Any]) -> dict[str, Any]:
        proposal = self.repository.edit_memory_proposal(proposal_id, value)
        self.repository.append_trace(
            "memory_proposal_edited", {"proposal_id": proposal_id}, actor="user"
        )
        return proposal

    def resolve_memory(self, proposal_id: str, approve: bool) -> dict[str, Any] | None:
        proposal = self.repository.get_memory_proposal(proposal_id)
        memory = self.repository.resolve_memory_proposal(proposal_id, approve)
        run_id = self.repository.get_feedback(proposal["feedback_id"])["run_id"]
        self.repository.append_trace(
            "memory_proposal_resolved",
            {"proposal_id": proposal_id, "decision": "approved" if approve else "rejected",
             "memory_id": memory["id"] if memory else None},
            run_id=run_id, actor="user",
        )
        return memory

    def update_memory(self, memory_id: str, value: dict[str, Any]) -> dict[str, Any]:
        memory = self.repository.update_memory(memory_id, value)
        self.repository.append_trace(
            "memory_updated", {"memory_id": memory_id}, actor="user"
        )
        return memory

    def delete_memory(self, memory_id: str) -> None:
        self.repository.delete_memory(memory_id)
        self.repository.append_trace(
            "memory_deleted", {"memory_id": memory_id}, actor="user"
        )
