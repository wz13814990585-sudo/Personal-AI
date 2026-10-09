"""Fixed local MCP stdio server for public YouTube video metadata only."""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

SERVER_NAME = "personal-ai-os-youtube-public"
PROTOCOL_VERSION = "2025-06-18"
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
DURATION = re.compile(r"^P(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+S)?)?$")
TOOLS = [
    {"name": "search_videos", "description": "Search public YouTube videos by text", "inputSchema": {
        "type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 120},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 3}},
        "required": ["query"], "additionalProperties": False}},
    {"name": "get_video_details", "description": "Read one public video's metadata", "inputSchema": {
        "type": "object", "properties": {"video_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{11}$"}},
        "required": ["video_id"], "additionalProperties": False}},
]


def _api(endpoint: str, params: dict[str, str]) -> dict[str, Any]:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("youtube_api_key_missing")
    url = "https://www.googleapis.com/youtube/v3/" + endpoint + "?" + urllib.parse.urlencode(
        {**params, "key": key}
    )
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=5) as response:
            raw = response.read(65537)
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 429}:
            raise RuntimeError("youtube_quota_or_permission") from None
        raise RuntimeError("youtube_http_error") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("youtube_transport_error") from None
    if len(raw) > 65536:
        raise ValueError("youtube_response_too_large")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("youtube_invalid_response") from None
    if not isinstance(result, dict):
        raise ValueError("youtube_invalid_response")
    return result


def _name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("youtube_invalid_metadata")
    cleaned = html.unescape(value).replace("\r", " ").replace("\n", " ").strip()
    if not cleaned or len(cleaned) > 300:
        raise ValueError("youtube_invalid_metadata")
    return cleaned


def _duration(value: Any) -> int:
    if not isinstance(value, str) or not DURATION.fullmatch(value) or value in {"P", "PT"}:
        raise ValueError("youtube_invalid_duration")
    days = re.search(r"(\d+)D", value)
    hours = re.search(r"(\d+)H", value)
    minutes = re.search(r"(\d+)M", value)
    seconds = re.search(r"(\d+)S", value)
    total = (int(days.group(1)) * 86400 if days else 0) + (int(hours.group(1)) * 3600 if hours else 0) + (int(minutes.group(1)) * 60 if minutes else 0) + (int(seconds.group(1)) if seconds else 0)
    if not 0 < total <= 86400:
        raise ValueError("youtube_invalid_duration")
    return total


def _base(video_id: str, snippet: dict[str, Any]) -> dict[str, Any]:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("youtube_invalid_video_id")
    return {"video_id": video_id, "title": _name(snippet.get("title")),
            "channel": _name(snippet.get("channelTitle")),
            "url": "https://www.youtube.com/watch?v=" + video_id,
            "source": "YouTube Data API v3"}


def search_videos(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    if set(arguments) - {"query", "max_results"}:
        raise ValueError("youtube_invalid_arguments")
    query, limit = arguments.get("query"), arguments.get("max_results", 3)
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120 or type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("youtube_invalid_arguments")
    data = _api("search", {"part": "snippet", "type": "video", "q": query.strip(),
                           "maxResults": str(limit), "safeSearch": "moderate"})
    items = data.get("items")
    if not isinstance(items, list) or len(items) > limit:
        raise ValueError("youtube_invalid_response")
    return [_base(item["id"]["videoId"], item["snippet"]) for item in items]


def get_video_details(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    if set(arguments) != {"video_id"} or not isinstance(arguments["video_id"], str) or not VIDEO_ID.fullmatch(arguments["video_id"]):
        raise ValueError("youtube_invalid_arguments")
    video_id = arguments["video_id"]
    data = _api("videos", {"part": "snippet,contentDetails,status", "id": video_id})
    items = data.get("items")
    if not isinstance(items, list) or len(items) > 1:
        raise ValueError("youtube_invalid_response")
    if not items:
        return []
    item = items[0]
    if item.get("status", {}).get("privacyStatus") != "public":
        return []
    result = _base(item["id"], item["snippet"])
    if result["video_id"] != video_id:
        raise ValueError("youtube_invalid_response")
    result["duration_seconds"] = _duration(item["contentDetails"]["duration"])
    result["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat()
    return [result]


def _response(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    identity = request.get("id")
    try:
        if method == "initialize":
            result: Any = {"protocolVersion": PROTOCOL_VERSION,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": SERVER_NAME, "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params", {})
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("youtube_invalid_arguments")
            if name == "search_videos":
                items = search_videos(arguments)
            elif name == "get_video_details":
                items = get_video_details(arguments)
            else:
                raise PermissionError("youtube_operation_not_allowed")
            result = {"content": [{"type": "text", "text": json.dumps(items, ensure_ascii=False)}],
                      "isError": False}
        else:
            raise ValueError("mcp_unknown_method")
        return {"jsonrpc": "2.0", "id": identity, "result": result}
    except Exception as exc:
        message = str(exc)
        if message not in {"youtube_api_key_missing", "youtube_quota_or_permission", "youtube_http_error",
                           "youtube_transport_error", "youtube_response_too_large", "youtube_invalid_response",
                           "youtube_invalid_metadata", "youtube_invalid_duration", "youtube_invalid_video_id",
                           "youtube_invalid_arguments", "youtube_operation_not_allowed", "mcp_unknown_method"}:
            message = "youtube_invalid_response"
        return {"jsonrpc": "2.0", "id": identity,
                "error": {"code": -32000, "message": message}}


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                continue
            response = _response(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except (ValueError, UnicodeDecodeError):
            continue


if __name__ == "__main__":
    main()
