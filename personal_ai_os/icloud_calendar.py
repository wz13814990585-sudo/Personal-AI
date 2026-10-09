"""A local, explicitly invoked EventKit bridge for one selected iCloud calendar."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .external import ExternalKnownFailure, ExternalUnknownResult


class MacEventKitBridge:
    def __init__(self, data_directory: Path):
        self.bundle = Path(data_directory) / "EventKitBridge.app"
        self.executable = self.bundle / "Contents" / "MacOS" / "EventKitBridge"
        self.source = Path(__file__).with_name("eventkit_bridge.swift")

    def ensure_built(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("eventkit_requires_macos")
        current = self.executable.exists() and self.executable.stat().st_mtime >= self.source.stat().st_mtime
        if current:
            verified = subprocess.run(
                ["codesign", "--verify", "--strict", str(self.bundle)],
                capture_output=True,
            )
            if verified.returncode == 0:
                return
            # Older helpers were only linker-signed as the temporary .building binary.
            self._sign_bundle()
            return
        compiler = shutil.which("swiftc")
        if compiler is None:
            raise RuntimeError("swift_compiler_unavailable")
        content = self.bundle / "Contents"
        self.executable.parent.mkdir(parents=True, exist_ok=True)
        info = {
            "CFBundleIdentifier": "local.personalaios.eventkitbridge",
            "CFBundleName": "Personal AI OS Calendar Bridge",
            "CFBundleExecutable": "EventKitBridge",
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
            "CFBundleShortVersionString": "1.0",
            "LSUIElement": True,
            "NSCalendarsFullAccessUsageDescription":
                "Personal AI OS 需要读取您选择的 iCloud 日历，并在您逐项确认后创建或修改事件。",
        }
        with (content / "Info.plist").open("wb") as handle:
            plistlib.dump(info, handle)
        cache = content / "ModuleCache"
        clang_cache = content / "ClangCache"
        cache.mkdir(exist_ok=True)
        clang_cache.mkdir(exist_ok=True)
        try:
            sdk = subprocess.run(
                ["xcrun", "--show-sdk-path"], capture_output=True, text=True,
                timeout=10, check=True,
            ).stdout.strip()
            temp_binary = self.executable.with_suffix(".building")
            result = subprocess.run(
                [compiler, "-parse-as-library", "-sdk", sdk,
                 "-module-cache-path", str(cache),
                 "-Xcc", f"-fmodules-cache-path={clang_cache}",
                 "-o", str(temp_binary), str(self.source)],
                capture_output=True, text=True, timeout=120,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            raise RuntimeError("eventkit_helper_build_failed") from exc
        if result.returncode:
            raise RuntimeError("eventkit_helper_build_failed: " + result.stderr[-2000:])
        os.replace(temp_binary, self.executable)
        self._sign_bundle()

    def _sign_bundle(self) -> None:
        signed = subprocess.run(
            ["codesign", "--force", "--sign", "-", "--identifier",
             "local.personalaios.eventkitbridge", str(self.bundle)],
            capture_output=True, text=True,
        )
        if signed.returncode:
            raise RuntimeError("eventkit_helper_sign_failed")

    def call(self, operation: str, **values: Any) -> dict[str, Any]:
        self.ensure_built()
        request = json.dumps({"operation": operation, **values}, ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
        try:
            result = subprocess.run(
                [str(self.executable)], input=request, capture_output=True,
                timeout=90 if operation == "list_calendars" else 30,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("eventkit_result_unknown") from exc
        if result.returncode or len(result.stdout) > 1_000_000:
            raise ExternalUnknownResult("eventkit_bridge_failed")
        try:
            response = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExternalUnknownResult("eventkit_invalid_response") from exc
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise ExternalUnknownResult("eventkit_invalid_response")
        if not response["ok"]:
            code = str(response.get("error_code", "eventkit_error"))
            if response.get("known_no_write"):
                raise ExternalKnownFailure(code)
            raise ExternalUnknownResult(code)
        data = response.get("result", response)
        if not isinstance(data, dict):
            raise ExternalUnknownResult("eventkit_invalid_response")
        return data


class ICloudCalendarAdapter:
    def __init__(self, bridge: MacEventKitBridge, calendar_id: str):
        self.bridge = bridge
        self.calendar_id = calendar_id

    @staticmethod
    def _account(account_id: str) -> None:
        if account_id != "icloud":
            raise PermissionError("icloud_account_required")

    @staticmethod
    def _time(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("invalid_calendar_time")
        try:
            instant = datetime.fromisoformat(value)
            if instant.tzinfo is None:
                raise ValueError
        except ValueError as exc:
            raise ValueError("invalid_calendar_time") from exc
        return instant.astimezone(timezone.utc).isoformat()

    def list_events(self, account_id: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        self._account(account_id)
        if set(query) != {"start_at_utc", "end_at_utc"}:
            raise ValueError("invalid_calendar_query")
        result = self.bridge.call(
            "list_events", calendar_id=self.calendar_id,
            start_at_utc=self._time(query["start_at_utc"]),
            end_at_utc=self._time(query["end_at_utc"]),
        )
        return result["items"]

    def prepare_write(self, operation: str, account_id: str, target: str,
                      payload: dict[str, Any]) -> dict[str, Any]:
        self._account(account_id)
        if operation == "create_event":
            if target != self.calendar_id:
                raise PermissionError("icloud_calendar_not_selected")
            merged = dict(payload)
            event_id = None
        elif operation == "update_event":
            initial = self.bridge.call("get_event", calendar_id=self.calendar_id,
                                       event_id=target)
            current = initial["event"]
            original_fingerprint = initial["fingerprint"]
            merged = {key: current[key] for key in (
                "title", "start_at_utc", "end_at_utc", "description", "location"
            )}
            if current.get("reminder_minutes") is not None:
                merged["reminder_minutes"] = current["reminder_minutes"]
            merged.update(payload)
            event_id = target
        else:
            raise ValueError("unsupported_calendar_operation")
        merged["start_at_utc"] = self._time(merged["start_at_utc"])
        merged["end_at_utc"] = self._time(merged["end_at_utc"])
        preview = self.bridge.call(
            "inspect_write", calendar_id=self.calendar_id, event_id=event_id,
            event=merged,
        )
        if preview["conflicts"]:
            details = ", ".join(
                f"{item.get('title', '事件')} ({item.get('start_at_utc', '')})"
                for item in preview["conflicts"][:5]
            )
            raise ExternalKnownFailure("icloud_calendar_conflict: " + details)
        if event_id:
            if preview["fingerprint"] != original_fingerprint:
                raise ExternalKnownFailure("icloud_event_changed")
            merged["expected_event_fingerprint"] = preview["fingerprint"]
            merged["selected_calendar_id"] = self.calendar_id
        return merged

    def create_event(self, account_id: str, calendar_id: str, event: dict[str, Any],
                     idempotency_key: str) -> dict[str, Any]:
        self._account(account_id)
        if calendar_id != self.calendar_id:
            raise ExternalKnownFailure("icloud_calendar_not_selected")
        return self.bridge.call("write_event", calendar_id=calendar_id, event=event,
                                idempotency_key=idempotency_key)

    def update_event(self, account_id: str, event_id: str, changes: dict[str, Any],
                     idempotency_key: str) -> dict[str, Any]:
        self._account(account_id)
        if changes.get("selected_calendar_id") != self.calendar_id:
            raise ExternalKnownFailure("icloud_calendar_not_selected")
        return self.bridge.call("write_event", calendar_id=self.calendar_id,
                                event_id=event_id, event=changes,
                                idempotency_key=idempotency_key)
