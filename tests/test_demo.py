"""Discussion demo starts with isolated data and cannot reach personal integrations."""

import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from streamlit.testing.v1 import AppTest

from personal_ai_os.demo import demo_environment, prepare_demo
from personal_ai_os.services import PersonalAIService


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def test_demo_environment_overrides_private_database_and_integrations(tmp_path):
    target = tmp_path / "demo.sqlite3"
    environment = demo_environment({
        "DATABASE_PATH": "/private/personal.sqlite3",
        "DEEPSEEK_API_KEY": "model-only-key",
        "GMAIL_ACCOUNT": "private@example.test",
        "GMAIL_OAUTH_CLIENT_PATH": "/private/oauth.json",
        "YOUTUBE_API_KEY": "youtube-secret",
        "NTFY_TOPIC_SECRET": "ab" * 32,
    }, target)
    assert environment["DATABASE_PATH"] == str(target.resolve())
    assert environment["DEEPSEEK_API_KEY"] == "model-only-key"
    assert environment["PERSONAL_AI_DEMO"] == "1"
    assert all(environment[key] == "" for key in (
        "GMAIL_ACCOUNT", "GMAIL_OAUTH_CLIENT_PATH", "YOUTUBE_API_KEY", "NTFY_TOPIC_SECRET",
    ))


def test_demo_seed_is_fresh_and_keeps_real_database_untouched(tmp_path):
    personal = tmp_path / "personal.sqlite3"
    personal.write_bytes(b"personal sentinel")
    path = tmp_path / "demo" / "demo.sqlite3"
    repo = prepare_demo(path, "Australia/Sydney", date(2026, 10, 9))
    assert personal.read_bytes() == b"personal sentinel"
    assert repo.get_setting("demo_instance") is True
    assert repo.get_setting("timezone") == "Australia/Sydney"
    assert len(repo.list_goals()) == 1
    assert len(repo.list_tasks()) == 1
    assert len(repo.list_habits()) == 1
    assert len(repo.list_time_blocks()) == 4
    assert repo.list_runs() == []
    assert len(repo.list_agent_configs()) == 6
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError, match="demo_database_already_exists"):
        prepare_demo(path, "Australia/Sydney", date(2026, 10, 9))
    assert len(repo.list_tasks()) == 1


def test_demo_service_rejects_external_access_and_system_notification(flow_system, monkeypatch):
    repo, _, gateway, _, harness, _ = flow_system
    monkeypatch.setenv("PERSONAL_AI_DEMO", "1")
    service = PersonalAIService(repo, gateway, harness)
    assert service.demo_mode
    for action in (
        service.available_icloud_calendars,
        service.connect_gmail,
        service.register_youtube_mcp,
        lambda: service.read_external("server", "read", "account", {}),
        lambda: service.propose_external_write("server", "write", "account", "target", {}),
        lambda: service.approve_external_write("proposal", 0),
        lambda: service.set_notification_preferences("22:00", "07:00", True),
    ):
        with pytest.raises(PermissionError, match="demo_"):
            action()


def test_demo_banner_and_external_controls_hidden_in_existing_pages(tmp_path, monkeypatch):
    path = tmp_path / "demo.sqlite3"
    today = datetime.now(ZoneInfo("Australia/Sydney")).date()
    prepare_demo(path, "Australia/Sydney", today)
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("PERSONAL_AI_DEMO", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GMAIL_ACCOUNT", raising=False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("NTFY_TOPIC_SECRET", raising=False)
    at = AppTest.from_file(APP).run(timeout=30)
    assert not at.exception
    assert any("演示模式" in item.value for item in at.info)
    assert any("整理 AI Agent 基础术语" in str(item.value) for item in at.dataframe)
    assert any("20 分钟运动任务" in item.value for item in at.text_area)
    at.switch_page("pages/03_agents.py").run(timeout=30)
    assert not at.exception
    assert not any(item.label == "授权并列出 iCloud 日历" for item in at.button)
    assert any("外部账号和 MCP 接入" in item.value for item in at.caption)
