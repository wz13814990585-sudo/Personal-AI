"""Launch a fresh, local discussion demo without using the personal database."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .agent_registry import AgentRegistry
from .config import load_config
from .contracts import GoalCreate, HabitCreate, TaskCreate, TimeBlockCreate
from .storage import Repository


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = PROJECT_ROOT / ".local" / "demo" / "runs"
DEMO_PORT = 8502
DEMO_REQUEST = (
    "明晚七点有 AI Agent 面试。请把核心概念复习安排在明天的可用时间，"
    "同时安排一个 20 分钟运动任务；明天 16:00–16:30 已有安排。"
)


def demo_environment(source: dict[str, str], database_path: Path) -> dict[str, str]:
    """Keep the model key, but detach all personal data and external accounts."""
    environment = source.copy()
    environment["DATABASE_PATH"] = str(database_path.resolve())
    environment["PERSONAL_AI_DEMO"] = "1"
    for key in ("GMAIL_ACCOUNT", "GMAIL_OAUTH_CLIENT_PATH", "YOUTUBE_API_KEY",
                "NTFY_TOPIC_SECRET"):
        environment[key] = ""
    return environment


def new_demo_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEMO_ROOT / f"{stamp}-{uuid4().hex[:8]}" / "demo.sqlite3"


def prepare_demo(database_path: Path, timezone_name: str,
                 today: date | None = None) -> Repository:
    """Seed only a new database. Every launch starts with the same business story."""
    if database_path.exists():
        raise FileExistsError("demo_database_already_exists")
    zone = ZoneInfo(timezone_name)
    today = today or datetime.now(zone).date()
    tomorrow = today + timedelta(days=1)

    def at(day: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), zone)

    repository = Repository(database_path)
    repository.initialize()
    AgentRegistry(repository)
    repository.set_setting("demo_instance", True)
    repository.set_setting("timezone", timezone_name)
    repository.set_setting("personal_goal", "准备 AI Agent 面试，并建立规律运动习惯")
    repository.set_setting("sleep_start", "23:00")
    repository.set_setting("sleep_end", "07:00")
    repository.create_goal(GoalCreate(
        title="准备 AI Agent 面试并保持日常运动",
        description="演示数据：把学习目标与生活安排放进同一份可审核计划。",
        due_at=at(tomorrow, 19),
    ))
    for day, start, end, label in (
        (today, 18, 20, "今晚的自主时间"),
        (tomorrow, 9, 11, "明天上午可用"),
        (tomorrow, 14, 18, "明天下午可用"),
    ):
        repository.create_time_block(TimeBlockCreate(
            kind="available", start_at=at(day, start), end_at=at(day, end), label=label,
        ))
    repository.create_time_block(TimeBlockCreate(
        kind="busy", start_at=at(tomorrow, 16), end_at=at(tomorrow, 16, 30),
        label="演示用：已有安排",
    ))
    repository.create_task(TaskCreate(
        title="整理 AI Agent 基础术语（演示任务）", estimated_minutes=15,
        due_at=at(today, 23, 55),
    ))
    repository.create_habit(HabitCreate(
        title="运动 20 分钟（演示习惯）", timezone=timezone_name, target_per_week=3,
    ))
    os.chmod(database_path, 0o600)
    return repository


def main() -> int:
    parser = argparse.ArgumentParser(description="Fresh, isolated Personal AI OS discussion demo")
    parser.add_argument("command", choices=["start", "prepare"])
    args = parser.parse_args()
    path = new_demo_path()
    environment = demo_environment(dict(os.environ), path)
    try:
        config = load_config(require_model=args.command == "start", environ=environment)
    except ValueError as exc:
        parser.error(str(exc))
    prepare_demo(path, config.timezone)
    print(f"演示数据库：{path}", flush=True)
    if args.command == "prepare":
        return 0
    print(f"演示地址：http://127.0.0.1:{DEMO_PORT}", flush=True)
    print("此入口只用独立演示数据；关闭终端后，下次启动会创建全新的演示场景。", flush=True)
    os.execve(sys.executable, [sys.executable, "-m", "personal_ai_os", "start",
                            "--port", str(DEMO_PORT)], environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
