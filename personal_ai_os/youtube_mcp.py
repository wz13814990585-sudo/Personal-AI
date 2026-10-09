"""Restricted stdio MCP client and code-owned YouTube read adapter."""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .youtube_mcp_server import PROTOCOL_VERSION, SERVER_NAME, TOOLS

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
OPERATIONS = {"search_videos": "read", "get_video_details": "read"}


class YouTubeMcpAdapter:
    operations = OPERATIONS

    def __init__(self, *, timeout_seconds: float = 9, command: list[str] | None = None,
                 api_key: str | None = None):
        self.timeout_seconds = timeout_seconds
        # The production command is fixed. A command override is for isolated fault tests only.
        self._command = command or [sys.executable, str(Path(__file__).with_name("youtube_mcp_server.py"))]
        self._api_key = api_key

    def _call(self, operation: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        if operation not in OPERATIONS:
            raise PermissionError("youtube_operation_not_allowed")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "YOUTUBE_API_KEY": self._api_key if self._api_key is not None else os.environ.get("YOUTUBE_API_KEY", "")}
        deadline = time.monotonic() + self.timeout_seconds
        try:
            process = subprocess.Popen(self._command, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env=env, close_fds=True)
        except OSError:
            raise RuntimeError("youtube_mcp_unavailable") from None
        try:
            init = self._exchange(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "personal-ai-os", "version": "1.0"}}}, deadline)["result"]
            if (init["protocolVersion"] != PROTOCOL_VERSION or
                init["serverInfo"]["name"] != SERVER_NAME or
                init["capabilities"] != {"tools": {}}):
                raise ValueError
            self._exchange(process, {"jsonrpc": "2.0", "method": "notifications/initialized"}, deadline)
            offered = self._exchange(process, {"jsonrpc": "2.0", "id": 2,
                                               "method": "tools/list"}, deadline)["result"]["tools"]
            if offered != TOOLS:
                raise ValueError
            response = self._exchange(process, {"jsonrpc": "2.0", "id": 3,
                                                "method": "tools/call", "params": {
                                                    "name": operation, "arguments": arguments}}, deadline)
            if "error" in response:
                code = response["error"].get("message")
                if code in {"youtube_api_key_missing", "youtube_quota_or_permission",
                            "youtube_http_error", "youtube_transport_error"}:
                    raise RuntimeError(code)
                raise RuntimeError("youtube_mcp_call_failed")
            result = response["result"]
            if result.get("isError") is not False or len(result["content"]) != 1 or result["content"][0]["type"] != "text":
                raise ValueError
            items = json.loads(result["content"][0]["text"])
            if not isinstance(items, list) or len(items) > 3:
                raise ValueError
            for item in items:
                self._validate_item(item, operation)
            return items
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError):
            raise ValueError("youtube_mcp_invalid_response") from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
            process.stdin.close()
            process.stdout.close()

    @staticmethod
    def _exchange(process: subprocess.Popen, request: dict[str, Any],
                  deadline: float) -> dict[str, Any]:
        try:
            process.stdin.write((json.dumps(request) + "\n").encode())
            process.stdin.flush()
            if "id" not in request:
                return {}
            line = b""
            while b"\n" not in line:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    raise TimeoutError("youtube_mcp_timeout")
                part = os.read(process.stdout.fileno(), 4096)
                if not part:
                    raise ValueError
                line += part
                if len(line) > 65536:
                    raise ValueError
            line, trailing = line.split(b"\n", 1)
            if trailing.strip():
                raise ValueError
            response = json.loads(line)
            if (not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or
                response.get("id") != request["id"]):
                raise ValueError
            return response
        except BrokenPipeError:
            raise ValueError("youtube_mcp_invalid_response") from None

    @staticmethod
    def _validate_item(item: Any, operation: str) -> None:
        if not isinstance(item, dict) or not VIDEO_ID.fullmatch(str(item.get("video_id", ""))):
            raise ValueError
        if set(item) != ({"video_id", "title", "channel", "url", "source"}
                        if operation == "search_videos" else
                        {"video_id", "title", "channel", "url", "source", "duration_seconds", "retrieved_at_utc"}):
            raise ValueError
        if item["url"] != "https://www.youtube.com/watch?v=" + item["video_id"] or item["source"] != "YouTube Data API v3":
            raise ValueError
        if any(not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 300
               for key in ("title", "channel")):
            raise ValueError
        if operation == "get_video_details" and (type(item["duration_seconds"]) is not int or
                                                  not 1 <= item["duration_seconds"] <= 86400 or
                                                  not isinstance(item["retrieved_at_utc"], str)):
            raise ValueError

    def read(self, operation: str, account_id: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        if account_id != "public" or not isinstance(query, dict):
            raise PermissionError("youtube_public_only")
        if operation == "search_videos":
            if set(query) - {"query", "max_results"} or not isinstance(query.get("query"), str) or not 1 <= len(query["query"].strip()) <= 120:
                raise ValueError("youtube_invalid_arguments")
            limit = query.get("max_results", 3)
            if type(limit) is not int or not 1 <= limit <= 3:
                raise ValueError("youtube_invalid_arguments")
        elif operation == "get_video_details":
            if set(query) != {"video_id"} or not isinstance(query["video_id"], str) or not VIDEO_ID.fullmatch(query["video_id"]):
                raise ValueError("youtube_invalid_arguments")
        else:
            raise PermissionError("youtube_operation_not_allowed")
        items = self._call(operation, query)
        if operation == "get_video_details" and items and items[0]["video_id"] != query["video_id"]:
            raise ValueError("youtube_mcp_invalid_response")
        return items
