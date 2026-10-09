"""Unified day view and explicit user approval for deterministic daily drafts."""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from personal_ai_os.contracts import DailyPlanAction
from personal_ai_os.display_zh import zh, zh_explanation, zh_issue, zh_source
from personal_ai_os.services import PersonalAIService
from .common import local_text, parse_datetime, show_error
from .proactive_view import render as render_proactive_view


def render(service: PersonalAIService) -> None:
    st.subheader("统一今日视图")
    selected_day = st.date_input("查看当地日期", datetime.now(ZoneInfo(service.timezone)).date())
    overview = service.daily_overview(selected_day)
    st.caption(f"{overview['local_date']} · {overview['timezone']}；未排程任务也会显示。")
    if overview["tasks"]:
        st.dataframe([{
            "任务": item["title"], "领域": zh(item["domain"]), "状态": zh(item["status"]),
            "来源": (f"重复规则 {item['recurrence_rule_id'][:8]}" if item["recurrence_rule_id"] else
                   f"Agent 运行 {item['source_run_id'][:8]}" if item["source_run_id"] else "手动任务"),
            "重复当地日期": item["recurrence_local_date"] or "",
            "开始": local_text(item["start_at_utc"], service.timezone),
            "结束": local_text(item["end_at_utc"], service.timezone),
            "未排程原因": zh_issue(item["unassigned_reason"]),
        } for item in overview["tasks"]], hide_index=True)
    else:
        st.caption("该日期没有任务。")
    if overview["time_blocks"]:
        st.dataframe([{
            "时间类型": zh(item["kind"]), "标签": item["label"],
            "开始": local_text(item["start_at_utc"], service.timezone),
            "结束": local_text(item["end_at_utc"], service.timezone),
        } for item in overview["time_blocks"]], hide_index=True)
    else:
        st.caption("该日期没有可用或占用时段。")
    if overview["habits"]:
        st.dataframe([{
            "习惯": item["title"], "状态": zh(item["status"]), "时区": item["timezone"],
            "习惯当地日期": item["habit_local_date"],
            "本周进度": f"{item['completed_this_week']}/{item['target_per_week']}",
            "当天打卡": "完成" if item["checked_today"] else "未完成",
        } for item in overview["habits"]], hide_index=True)

    st.subheader("应用内提醒")
    notifications = service.list_notifications()
    if notifications:
        st.dataframe([{
            "任务": item["title"], "状态": zh(item["status"]),
            "系统通知": zh(item["system_status"]),
            "原提醒时间": local_text(item["original_due_at_utc"], service.timezone),
            "收件时间": local_text(item["visible_at_utc"], service.timezone),
            "错误": zh(item["error"]), "已读": "是" if item["read_at_utc"] else "否",
        } for item in notifications], hide_index=True)
        unread = [item for item in notifications if not item["read_at_utc"]]
        if unread:
            selected_notification = st.selectbox(
                "选择提醒标记已读", [item["id"] for item in unread],
                format_func=lambda value: next(item["title"] for item in unread if item["id"] == value),
            )
            if st.button("标记提醒已读"):
                service.mark_notification_read(selected_notification)
                st.rerun()
    else:
        st.caption("暂无提醒。启动本机后台任务进程后，到期提醒会在这里出现。")

    st.subheader("每日复盘")
    st.caption("复盘备注仅保存在本地复盘，不会直接进入长期记忆。已完成任务的反馈仍需 Memory Agent 提取和人工审核。")
    reviews = [item for item in service.list_daily_reviews() if item["local_date"] == selected_day.isoformat()
               and item["timezone"] == service.timezone]
    if not reviews and st.button("创建当日复盘"):
        try:
            service.create_daily_review(selected_day)
            st.rerun()
        except Exception as exc:
            show_error(exc)
    if reviews:
        review = reviews[0]
        snapshot = review["snapshot"]
        st.write(f"完成任务 {snapshot['completed_tasks']} · 未完成任务 {snapshot['unfinished_tasks']} · "
                 f"习惯打卡 {snapshot['checked_habits']}/{snapshot['active_habits']} · "
                 f"计划冲突 {snapshot.get('plan_conflicts', 0)}")
        with st.form(f"review_note_{review['id']}_{review['revision']}"):
            note = st.text_area("复盘备注", value=review["note"])
            save_note = st.form_submit_button("保存复盘备注")
        if save_note:
            try:
                service.save_daily_review_note(review["id"], review["revision"], note)
                st.rerun()
            except Exception as exc:
                show_error(exc)
        completed = [item for item in service.list_tasks() if item["status"] == "completed"]
        if completed:
            with st.form(f"review_feedback_{review['id']}"):
                task_id = st.selectbox("关联已完成任务", [item["id"] for item in completed],
                    format_func=lambda value: next(item["title"] for item in completed if item["id"] == value))
                body = st.text_area("复盘中的任务反馈")
                send = st.form_submit_button("提交任务反馈给 Memory Agent", disabled=not service.model_ready)
            if send:
                try:
                    service.submit_feedback(task_id, body)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)

    render_proactive_view(service)

    if st.button("生成每日草案"):
        try:
            draft = service.propose_daily_plan(selected_day)
            st.session_state.selected_daily_plan_id = draft.id
            st.rerun()
        except Exception as exc:
            show_error(exc)
    plans = [item for item in service.list_daily_plans() if item.local_date == selected_day]
    if not plans:
        return
    ids = [item.id for item in plans]
    selected = st.session_state.get("selected_daily_plan_id")
    if selected not in ids:
        selected = ids[0]
    plan_id = st.selectbox(
        "查看每日草案", ids, index=ids.index(selected),
        format_func=lambda item: next(
            f"{plan.local_date} · 版本 {plan.revision} · {zh(plan.status)} · {item[:8]}"
            for plan in plans if plan.id == item
        ),
    )
    st.session_state.selected_daily_plan_id = plan_id
    draft = service.get_daily_plan(plan_id)
    st.write(f"每日草案 ID：`{draft.id}` · 版本：{draft.revision} · 状态：**{zh(draft.status)}**")
    if draft.actions:
        st.dataframe([{
            "动作": zh(item.kind), "任务": item.title, "领域": zh(item.domain),
            "来源": zh_source(item.source), "优先级": zh(item.priority), "预计分钟": item.estimated_minutes,
            "原开始": local_text(item.old_start_at.isoformat() if item.old_start_at else None, draft.timezone),
            "原结束": local_text(item.old_end_at.isoformat() if item.old_end_at else None, draft.timezone),
            "新开始": local_text(item.new_start_at.isoformat() if item.new_start_at else None, draft.timezone),
            "新结束": local_text(item.new_end_at.isoformat() if item.new_end_at else None, draft.timezone),
            "依据": zh_issue(zh_explanation(item.reason)),
            "未排程原因": zh_issue(item.unassigned_reason),
        } for item in draft.actions], hide_index=True)
    for conflict in draft.conflicts:
        st.warning(zh_issue(conflict))
    if draft.status != "draft":
        return

    with st.form(f"edit_daily_{draft.id}_{draft.revision}"):
        edited = []
        for index, item in enumerate(draft.actions):
            st.markdown(f"**{zh(item.kind)} · {item.title} · {zh_source(item.source)}**")
            keep = st.checkbox(f"保留每日动作 {index + 1}", value=True)
            start = st.text_input(f"每日新开始 {index + 1}（ISO 8601，可留空）",
                                  local_text(item.new_start_at.isoformat() if item.new_start_at else None, draft.timezone))
            end = st.text_input(f"每日新结束 {index + 1}（ISO 8601，可留空）",
                                local_text(item.new_end_at.isoformat() if item.new_end_at else None, draft.timezone))
            reason = st.text_input(f"每日依据 {index + 1}", item.reason)
            edited.append((item, keep, start, end, reason))
        save = st.form_submit_button("保存每日草案修改")
    if save:
        try:
            actions = [DailyPlanAction.model_validate({
                **item.model_dump(), "new_start_at": parse_datetime(start),
                "new_end_at": parse_datetime(end), "reason": reason,
                "unassigned_reason": None if start.strip() else item.unassigned_reason,
            }) for item, keep, start, end, reason in edited if keep]
            service.edit_daily_plan(draft.id, draft.revision, actions)
            st.rerun()
        except Exception as exc:
            show_error(exc)

    with st.expander("向每日草案添加任务"):
        with st.form(f"add_daily_task_{draft.id}_{draft.revision}"):
            title = st.text_input("每日新增任务标题")
            domain = st.selectbox("每日新增领域", ["general", "study", "life"], format_func=zh)
            priority = st.selectbox("每日新增优先级", ["low", "medium", "high"], index=1,
                                    format_func=zh)
            minutes = st.number_input("每日新增预计分钟", min_value=1, value=30)
            due = st.text_input("每日新增期限（ISO 8601，可留空）")
            start = st.text_input("每日新增开始（ISO 8601）")
            end = st.text_input("每日新增结束（ISO 8601）")
            reason = st.text_input("每日新增依据", "用户手动添加")
            add = st.form_submit_button("添加新增任务动作")
        if add:
            try:
                action = DailyPlanAction(
                    action_id=uuid4().hex, kind="create_task", title=title, domain=domain,
                    priority=priority, estimated_minutes=minutes, due_at=parse_datetime(due),
                    new_start_at=parse_datetime(start), new_end_at=parse_datetime(end),
                    source="user_daily_plan", reason=reason,
                )
                service.edit_daily_plan(draft.id, draft.revision, [*draft.actions, action])
                st.rerun()
            except Exception as exc:
                show_error(exc)

    candidates = [item for item in service.list_tasks()
                  if item["status"] != "completed"
                  and item["id"] not in {action.task_id for action in draft.actions}]
    if candidates:
        with st.expander("向每日草案添加重排"):
            with st.form(f"add_daily_move_{draft.id}_{draft.revision}"):
                task_id = st.selectbox("选择重排任务", [item["id"] for item in candidates],
                                       format_func=lambda value: next(
                                           item["title"] for item in candidates if item["id"] == value
                                       ))
                start = st.text_input("重排新开始（ISO 8601）")
                end = st.text_input("重排新结束（ISO 8601）")
                reason = st.text_input("重排依据", "用户提出调整")
                add = st.form_submit_button("添加重排动作")
            if add:
                try:
                    task = next(item for item in candidates if item["id"] == task_id)
                    action = DailyPlanAction(
                        action_id=uuid4().hex, kind="reschedule_task", task_id=task_id,
                        title=task["title"], domain=task["domain"], priority=task["priority"],
                        estimated_minutes=task["estimated_minutes"],
                        due_at=parse_datetime(task["due_at_utc"] or ""),
                        old_start_at=parse_datetime(task["start_at_utc"] or ""),
                        old_end_at=parse_datetime(task["end_at_utc"] or ""), old_version=task["version"],
                        new_start_at=parse_datetime(start), new_end_at=parse_datetime(end),
                        source=(f"recurrence:{task['recurrence_rule_id']}" if task["recurrence_rule_id"]
                                else f"task:{task_id}"), reason=reason,
                    )
                    service.edit_daily_plan(draft.id, draft.revision, [*draft.actions, action])
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
    if st.button("确认每日计划", disabled=bool(draft.conflicts) or not draft.actions):
        try:
            service.approve_daily_plan(draft.id, draft.revision)
            st.rerun()
        except Exception as exc:
            show_error(exc)
