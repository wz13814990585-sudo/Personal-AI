"""Process configuration. Model calls are restricted to DeepSeek."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ALLOWED_DEEPSEEK_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})


@dataclass(frozen=True)
class AppConfig:
    database_path: Path
    timezone: str
    deepseek_model_id: str
    deepseek_api_key: str | None
    model_timeout_seconds: int = 45
    run_timeout_seconds: int = 240
    max_agent_steps: int = 6
    max_custom_agent_steps: int = 4
    max_tool_calls_per_agent: int = 4


def load_config(
    require_model: bool = False, environ: Mapping[str, str] | None = None
) -> AppConfig:
    source = os.environ if environ is None else environ
    model_id = source.get("DEEPSEEK_MODEL_ID", "deepseek-flash").strip()
    if model_id not in ALLOWED_DEEPSEEK_MODELS:
        raise ValueError("DEEPSEEK_MODEL_ID must name an allowed DeepSeek model")

    timezone = source.get("APP_TIMEZONE", "Australia/Sydney").strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Invalid APP_TIMEZONE: {timezone}") from exc

    api_key = source.get("DEEPSEEK_API_KEY", "").strip() or None
    if require_model and api_key is None:
        raise ValueError("DEEPSEEK_API_KEY is required for real model calls")

    database_path = Path(
        source.get("DATABASE_PATH", ".local/personal_ai_os.sqlite3")
    ).expanduser()
    return AppConfig(database_path, timezone, model_id, api_key)
