"""Chinese presentation labels for persisted protocol values.

Keep database values, API payloads, permission names and model contracts unchanged.
Only call these helpers when rendering text for a person.
"""

from __future__ import annotations

from typing import Any
import re


_LABELS = {
    # Lifecycle and execution states.
    "pending": "待处理", "waiting_approval": "待确认", "draft": "草案",
    "in_progress": "进行中", "running": "运行中", "success": "成功",
    "succeeded": "已完成", "completed": "已完成", "failed": "失败",
    "rejected": "已拒绝", "cancelled": "已取消", "canceled": "已取消",
    "accepted": "已接受", "approved": "已批准", "committed": "已确认",
    "active": "已启用", "paused": "已暂停", "archived": "已归档",
    "revoked": "已撤销", "expired": "已过期", "unknown": "结果未知",
    "delivered": "已送达", "missed": "已错过", "skipped": "已跳过",
    "blocked": "已阻止", "not_started": "未开始", "not_run": "未运行",
    "disabled": "已关闭", "unsupported": "设备不支持", "attempting": "执行中",
    "timeout": "超时", "leased": "处理中", "proposal": "提案", "advice": "只读建议",
    # Task, schedule and permission values.
    "low": "低", "medium": "中", "high": "高",
    "general": "通用", "study": "学习", "life": "生活",
    "available": "可用", "busy": "占用", "daily": "每天",
    "weekly_days": "指定星期", "read": "只读", "write": "写入",
    "calendar": "日历", "mail": "邮件", "mcp": "MCP 服务",
    "create_task": "新增任务", "reschedule_task": "重排任务",
    "planning": "规划", "feedback": "反馈提取",
    "study_time_avoid": "学习避让时段", "text_preference": "文字偏好",
    "overdue_task": "逾期任务", "habit_gap": "习惯进度差距",
    "daily_review": "每日复盘",
    "overdue_days": "逾期识别延迟（天）",
    "habit_gap_min": "习惯差距触发次数",
    "review_unfinished_min": "复盘未完成触发项数",
    # Built-in roles and external operations. These are display-only aliases.
    "orchestrator": "编排智能体", "memory": "记忆智能体",
    "learning": "学习智能体", "schedule": "日程智能体",
    "task": "任务智能体", "custom": "自定义智能体",
    "create_event": "创建日历事件", "update_event": "修改日历事件",
    "list_events": "列出日历事件", "read_events": "读取日历事件",
    "list_messages": "列出邮件", "get_message": "读取邮件正文",
    "send_message": "发送邮件", "search_videos": "搜索视频",
    "get_video_details": "读取视频详情",
    # Trace events.
    "step_started": "步骤开始", "step_succeeded": "步骤成功",
    "step_failed": "步骤失败", "step_skipped": "步骤跳过",
    "tool_called": "调用工具", "tool_denied": "工具调用被拒绝",
    "run_started": "运行开始", "run_succeeded": "运行成功",
    "run_failed": "运行失败", "model_request": "模型请求",
    "model_response": "模型响应", "approval": "用户确认",
    "draft_ready": "草案已生成", "checkpoint_reused": "已复用检查点",
    "step_attempt_retry": "重试步骤", "tool_failed": "工具执行失败",
    "read_succeeded": "读取成功", "read_failed": "读取失败",
    "write_proposal_reused": "复用写入草案",
    "write_approval_denied": "写入确认被拒绝",
    "server_status_changed": "服务器状态已变更",
    "operation_status_changed": "操作状态已变更",
    "operation_denied": "操作被拒绝",
    "user": "用户", "harness": "执行器", "system": "系统",
    "model": "模型", "worker": "后台任务进程",
    # Tool names shown in the permission console and trace.
    "list_agent_capabilities": "查看智能体能力", "read_goal_summary": "读取目标摘要",
    "search_approved_memories": "检索已批准记忆", "read_feedback": "读取反馈",
    "read_goals": "读取目标", "read_task_progress": "读取任务进度",
    "read_settings": "读取设置", "read_time_blocks": "读取时间段",
    "read_scheduled_tasks": "读取已排程任务", "read_tasks": "读取任务",
    "commit_approved_plan": "提交已确认计划",
    # Frequently surfaced validation codes. Unknown codes remain inspectable.
    "stale_daily_revision": "每日草案版本已过期",
    "stale_plan_revision": "计划草案版本已过期",
    "stale_revision": "版本已过期", "task_version_conflict": "任务版本冲突",
    "schedule_conflict": "时间冲突", "invalid_model_output": "模型输出格式无效",
    "permission_denied": "权限不足", "model_timeout": "模型请求超时",
    "no_availability": "没有可用时间", "outside_availability": "不在可用时间内",
    "busy_overlap": "与占用时间冲突", "task_overlap": "与已有任务冲突",
    "after_deadline": "超过期限", "avoided_local_time": "处于偏好避让时段",
    "no_legal_time": "没有合法排程时间", "insufficient_window": "可用时段不足",
    "nonexistent_local_time": "当地时间不存在（夏令时）",
    "ambiguous_local_time": "当地时间有歧义（夏令时）",
    "nonexistent_local_calendar_time": "日历当地时间不存在（夏令时）",
    "ambiguous_local_calendar_time": "日历当地时间有歧义（夏令时）",
    "unassigned": "未排程", "wrong_local_date": "不属于所选当地日期",
    "crosses_local_day": "跨越当地日期",
}

_DISPLAY_KEYS = {
    "agent": "智能体", "role": "角色", "step_id": "步骤编号",
    "tool": "工具", "tool_name": "工具名称", "status": "状态",
    "error_code": "错误代码", "reason": "原因", "count": "数量",
    "run_id": "运行编号", "proposal_id": "草案编号",
    "duration_ms": "耗时（毫秒）", "attempt": "尝试次数",
    "message": "信息", "result": "结果", "source": "来源",
    "summary": "摘要", "recommendations": "建议", "limitations": "局限",
    "action": "行动", "source_ids": "来源编号", "evidence": "依据",
}


def zh(value: Any) -> str:
    """Translate a known code without ever changing the underlying value."""
    if value is None:
        return ""
    raw = str(value)
    return _LABELS.get(raw, raw)


def zh_list(values: list[str] | tuple[str, ...]) -> str:
    return "、".join(zh(value) for value in values)


def zh_role(value: str) -> str:
    if value == "life":
        return "生活智能体"
    return zh(value)


def zh_step(value: str) -> str:
    matched = re.fullmatch(r"(custom|learning|life|schedule|task|memory|orchestrator)_(\d+)", value)
    if matched:
        return f"{zh_role(matched.group(1))}步骤 {matched.group(2)}"
    return zh_role(value)


def zh_source(value: str) -> str:
    if value == "user_daily_plan":
        return "用户添加的每日计划"
    if value.startswith("recurrence:"):
        return "重复规则 " + value.partition(":")[2][:8]
    if value.startswith("task:"):
        return "已有任务 " + value.partition(":")[2][:8]
    return zh_step(value)


def zh_policy(policy: dict[str, Any]) -> str:
    return "；".join(f"{zh(key)}：{value}" for key, value in policy.items())


def zh_explanation(value: str) -> str:
    """Localize fixed model/harness terminology in displayed explanation prose."""
    text = re.sub(
        r"^(memory|learning|life|schedule|task)(:\s*| 来源)",
        lambda match: zh_role(match.group(1)) + ("：" if match.group(2).startswith(":") else " 来源"),
        value,
    )
    parts = re.split(r"(https?://\S+)", text)
    for index in range(0, len(parts), 2):
        for code, label in (
            ("due_at", "截止时间"), ("priority", "优先级"),
            ("high", "高"), ("medium", "中"), ("low", "低"),
        ):
            parts[index] = re.sub(
                rf"(?<![A-Za-z0-9_/:]){code}(?![A-Za-z0-9_/])", label, parts[index]
            )
        for name, label in (
            ("Orchestrator Agent", "编排智能体"), ("Memory Agent", "记忆智能体"),
            ("Learning Agent", "学习智能体"), ("Life Agent", "生活智能体"),
            ("Schedule Agent", "日程智能体"), ("Task Agent", "任务智能体"),
        ):
            parts[index] = parts[index].replace(name, label)
    return "".join(parts)


def zh_issue(value: str | None) -> str:
    """Translate known schedule issue codes inside compound conflict messages."""
    if not value:
        return ""
    return re.sub(
        r"(?<![A-Za-z0-9_])([a-z][a-z_]+)(?![A-Za-z0-9_])",
        lambda match: zh(match.group(1)), value,
    )


def zh_data(value: Any) -> Any:
    """Translate known codes in read-only trace display data."""
    if isinstance(value, dict):
        return {_DISPLAY_KEYS.get(key, key): zh_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [zh_data(item) for item in value]
    if isinstance(value, str):
        return zh(value)
    return value
