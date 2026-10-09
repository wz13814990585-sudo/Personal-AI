"""Small UI helpers shared by pages."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from personal_ai_os.display_zh import zh
from personal_ai_os.external import ExternalKnownFailure


def show_error(exc: Exception) -> None:
    if isinstance(exc, (ValueError, KeyError, PermissionError, ExternalKnownFailure)):
        code = str(exc).strip("'\"")
        translated = zh(code)
        st.error(translated if translated != code else "操作未完成，请核对输入或查看执行轨迹。")
        if translated == code:
            with st.expander("技术错误代码"):
                st.code(code)
    else:
        st.error("操作失败。详情可在执行轨迹查看。")


def parse_datetime(value: str) -> datetime | None:
    if not value.strip():
        return None
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间需要包含时区偏移，例如 2026-10-09T14:00+11:00")
    return parsed


def local_text(utc_text: str | None, timezone_name: str) -> str:
    if not utc_text:
        return ""
    return datetime.fromisoformat(utc_text).astimezone(ZoneInfo(timezone_name)).isoformat(timespec="minutes")


def now_local_text(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).replace(second=0, microsecond=0).isoformat(timespec="minutes")
