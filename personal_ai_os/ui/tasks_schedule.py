"""Manual task and availability management, completion and feedback."""

from __future__ import annotations

import streamlit as st
import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from personal_ai_os.contracts import RecurrenceRuleCreate, TaskCreate, TimeBlockCreate
from personal_ai_os.display_zh import zh, zh_issue
from personal_ai_os.services import PersonalAIService
from .common import local_text, now_local_text, parse_datetime, show_error


def render(service: PersonalAIService) -> None:
    st.title("任务与日程")
    st.caption(f"时间按 {service.timezone} 显示；输入时间需带时区偏移。")
    goals = service.list_goals()
    goal_choices = {"无目标": None, **{goal["title"]: goal["id"] for goal in goals}}
    with st.expander("创建任务", expanded=False):
        with st.form("create_task"):
            title = st.text_input("任务标题")
            goal_name = st.selectbox("关联目标", list(goal_choices))
            priority = st.selectbox("优先级", ["low", "medium", "high"], index=1, format_func=zh)
            minutes = st.number_input("预计分钟", min_value=1, value=30)
            due = st.text_input("期限（ISO 8601，可留空）")
            start = st.text_input("开始时间（ISO 8601，可留空）")
            end = st.text_input("结束时间（ISO 8601，可留空）")
            save = st.form_submit_button("创建任务")
        if save:
            try:
                service.create_task(TaskCreate(
                    title=title, goal_id=goal_choices[goal_name], priority=priority,
                    estimated_minutes=minutes, due_at=parse_datetime(due),
                    start_at=parse_datetime(start), end_at=parse_datetime(end),
                ))
                st.rerun()
            except Exception as exc:
                show_error(exc)

    tasks = service.list_tasks()
    instances = {row["task_id"]: row for row in service.list_recurrence_instances()}
    st.subheader("任务列表")
    if tasks:
        st.dataframe([
            {"标题": task["title"], "领域": zh(task["domain"]), "状态": zh(task["status"]),
             "优先级": zh(task["priority"]),
             "预计分钟": task["estimated_minutes"],
             "开始": local_text(task["start_at_utc"], service.timezone),
             "结束": local_text(task["end_at_utc"], service.timezone),
             "未排程原因": zh_issue(instances.get(task["id"], {}).get("unassigned_reason"))}
            for task in tasks
        ], hide_index=True)
        selected_id = st.selectbox(
            "选择任务", [task["id"] for task in tasks],
            format_func=lambda item: next(task["title"] for task in tasks if task["id"] == item),
        )
        task = next(item for item in tasks if item["id"] == selected_id)
        if task.get("resource_url"):
            st.link_button(
                f"学习资料 · {task['resource_channel']} · "
                f"{task['resource_duration_seconds'] // 60} 分钟 · {task['resource_source']}",
                task["resource_url"],
            )
        reminder = service.get_task_reminder(selected_id)
        if task["start_at_utc"] and task["status"] != "completed":
            with st.form(f"task_reminder_{selected_id}"):
                lead = st.number_input("提前提醒分钟", min_value=0, max_value=10080,
                                       value=reminder["lead_minutes"] if reminder else 15)
                enabled = st.checkbox("启用任务提醒", value=bool(reminder["enabled"]) if reminder else False)
                save_reminder = st.form_submit_button("保存提醒设置")
            if save_reminder:
                try:
                    service.set_task_reminder(selected_id, lead, enabled)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
        with st.form(f"edit_task_{selected_id}"):
            title = st.text_input("修改标题", task["title"])
            priority = st.selectbox(
                "修改优先级", ["low", "medium", "high"],
                index=["low", "medium", "high"].index(task["priority"]),
                format_func=zh,
            )
            minutes = st.number_input("修改预计分钟", min_value=1, value=task["estimated_minutes"])
            due = st.text_input("修改期限（ISO 8601）", local_text(task["due_at_utc"], service.timezone))
            start = st.text_input("修改开始时间（ISO 8601）", local_text(task["start_at_utc"], service.timezone))
            end = st.text_input("修改结束时间（ISO 8601）", local_text(task["end_at_utc"], service.timezone))
            status = st.selectbox(
                "任务状态", ["pending", "in_progress", "completed"],
                index=["pending", "in_progress", "completed"].index(task["status"]),
                format_func=zh,
            )
            update = st.form_submit_button("保存任务")
        if update:
            try:
                service.update_task(selected_id, TaskCreate(
                    title=title, goal_id=task["goal_id"], priority=priority,
                    estimated_minutes=minutes, due_at=parse_datetime(due),
                    start_at=parse_datetime(start), end_at=parse_datetime(end),
                ))
                if status != task["status"]:
                    service.set_task_status(selected_id, status)
                st.rerun()
            except Exception as exc:
                show_error(exc)
        left, right = st.columns(2)
        with left:
            if task["status"] != "completed" and st.button("标记任务完成", key=f"complete_{selected_id}"):
                try:
                    service.complete_task(selected_id)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
        with right:
            if st.button("删除任务", key=f"delete_{selected_id}"):
                try:
                    service.delete_task(selected_id)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
        if task["status"] == "completed":
            with st.form(f"feedback_{selected_id}"):
                body = st.text_area("完成后的反馈", placeholder="不要在早上安排学习，下午更合适。")
                submit = st.form_submit_button("提交反馈并提取候选记忆", disabled=not service.model_ready)
            if submit:
                try:
                    with st.status("Memory Agent 正在提取候选记忆") as progress:
                        feedback, proposals = service.submit_feedback(selected_id, body)
                        progress.update(label=f"已保存反馈；待审核候选 {len(proposals)} 条", state="complete")
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
    else:
        st.caption("暂无任务。")

    st.subheader("重复任务规则")
    st.caption("保存规则即确认授权；本机后台任务进程会自动扫描，也可手动生成未来七天。既有规则沿用保存时的时区。")
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    with st.form("create_recurrence"):
        rule_title = st.text_input("重复任务标题")
        domain = st.selectbox("重复任务领域", ["general", "study", "life"], format_func=zh)
        frequency = st.selectbox("重复频率", ["daily", "weekly_days"], format_func=zh)
        selected_days = st.multiselect("重复星期", weekdays)
        rule_zone = st.text_input("规则时区", service.timezone)
        rule_start = st.text_input("规则起始日期（YYYY-MM-DD）", datetime.now(ZoneInfo(service.timezone)).date().isoformat())
        rule_end = st.text_input("规则结束日期（可留空）")
        preferred = st.text_input("首选当地时间（HH:MM，可留空）")
        rule_minutes = st.number_input("重复任务分钟", min_value=1, value=30)
        rule_priority = st.selectbox("重复任务优先级", ["low", "medium", "high"], index=1,
                                     format_func=zh)
        create_rule = st.form_submit_button("确认并创建重复规则")
    if create_rule:
        try:
            service.create_recurrence_rule(RecurrenceRuleCreate(
                title=rule_title, domain=domain, frequency=frequency,
                weekdays=[weekdays.index(value) for value in selected_days], timezone=rule_zone,
                start_date=date.fromisoformat(rule_start),
                end_date=date.fromisoformat(rule_end) if rule_end.strip() else None,
                preferred_time=time.fromisoformat(preferred) if preferred.strip() else None,
                estimated_minutes=rule_minutes, priority=rule_priority,
            ))
            st.rerun()
        except Exception as exc:
            show_error(exc)
    for rule in service.list_recurrence_rules():
        with st.expander(f"{rule['title']} · {zh(rule['status'])} · {rule['timezone']}"):
            st.caption("修改仅影响尚未生成的日期；历史实例保持原样。")
            with st.form(f"edit_recurrence_{rule['id']}"):
                title = st.text_input("修改重复任务标题", rule["title"])
                domain = st.selectbox("修改重复任务领域", ["general", "study", "life"],
                                      index=["general", "study", "life"].index(rule["domain"]),
                                      format_func=zh)
                frequency = st.selectbox("修改重复频率", ["daily", "weekly_days"],
                                         index=["daily", "weekly_days"].index(rule["frequency"]),
                                         format_func=zh)
                days = st.multiselect("修改重复星期", weekdays,
                                      default=[weekdays[i] for i in json.loads(rule["weekdays_json"])])
                timezone_name = st.text_input("修改规则时区", rule["timezone"])
                start_day = st.text_input("修改规则起始日期", rule["start_date"])
                end_day = st.text_input("修改规则结束日期", rule["end_date"] or "")
                preferred_time = st.text_input("修改首选当地时间", rule["preferred_time"] or "")
                minutes = st.number_input("修改重复任务分钟", min_value=1, value=rule["estimated_minutes"])
                priority = st.selectbox("修改重复任务优先级", ["low", "medium", "high"],
                                        index=["low", "medium", "high"].index(rule["priority"]),
                                        format_func=zh)
                save_rule = st.form_submit_button("保存重复规则")
            if save_rule:
                try:
                    service.update_recurrence_rule(rule["id"], RecurrenceRuleCreate(
                        title=title, domain=domain, frequency=frequency,
                        weekdays=[weekdays.index(value) for value in days], timezone=timezone_name,
                        start_date=date.fromisoformat(start_day),
                        end_date=date.fromisoformat(end_day) if end_day.strip() else None,
                        preferred_time=time.fromisoformat(preferred_time) if preferred_time.strip() else None,
                        estimated_minutes=minutes, priority=priority,
                    ))
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            left, right = st.columns(2)
            with left:
                next_status = "paused" if rule["status"] == "active" else "active"
                if st.button("暂停重复规则" if next_status == "paused" else "恢复重复规则",
                             key=f"rule_status_{rule['id']}"):
                    try:
                        service.set_recurrence_rule_status(rule["id"], next_status)
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)
            with right:
                if st.button("生成未来七天任务", key=f"scan_rule_{rule['id']}"):
                    try:
                        service.generate_recurrence_instances(rule["id"])
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)
            rows = service.list_recurrence_instances(rule["id"])
            if rows:
                st.dataframe([{"当地日期": row["local_date"],
                               "未排程原因": zh_issue(row["unassigned_reason"]) or "已排程"}
                              for row in rows], hide_index=True)

    st.subheader("可用与占用时间")
    st.caption("修改可用时间会生成待确认的每日重排草案；既有任务时间在确认草案前保持原样。")
    blocks = service.list_time_blocks()
    if blocks:
        st.dataframe([
            {"类型": zh(row["kind"]), "标签": row["label"],
             "开始": local_text(row["start_at_utc"], service.timezone),
             "结束": local_text(row["end_at_utc"], service.timezone)}
            for row in blocks
        ], hide_index=True)
    with st.form("create_block"):
        kind = st.selectbox("时间类型", ["available", "busy"], format_func=zh)
        label = st.text_input("时间标签")
        default = now_local_text(service.timezone)
        start = st.text_input("时间开始（ISO 8601）", default)
        end = st.text_input("时间结束（ISO 8601）", default)
        add = st.form_submit_button("添加时间段")
    if add:
        try:
            service.create_time_block(TimeBlockCreate(
                kind=kind, label=label, start_at=parse_datetime(start), end_at=parse_datetime(end)
            ))
            st.rerun()
        except Exception as exc:
            show_error(exc)
    if blocks:
        block_id = st.selectbox(
            "选择时间段编辑", [row["id"] for row in blocks],
            format_func=lambda item: next(
                f"{zh(row['kind'])} · {row['label'] or row['start_at_utc']}"
                for row in blocks if row["id"] == item
            ),
        )
        block = next(row for row in blocks if row["id"] == block_id)
        with st.form(f"edit_block_{block_id}"):
            kind = st.selectbox(
                "修改时间类型", ["available", "busy"],
                index=["available", "busy"].index(block["kind"]),
                format_func=zh,
            )
            label = st.text_input("修改时间标签", block["label"])
            start = st.text_input("修改时间开始", local_text(block["start_at_utc"], service.timezone))
            end = st.text_input("修改时间结束", local_text(block["end_at_utc"], service.timezone))
            update = st.form_submit_button("保存时间段")
        if update:
            try:
                service.update_time_block(block_id, TimeBlockCreate(
                    kind=kind, label=label, start_at=parse_datetime(start), end_at=parse_datetime(end)
                ))
                st.rerun()
            except Exception as exc:
                show_error(exc)
        if st.button("删除时间段", key=f"delete_block_{block_id}"):
            try:
                service.delete_time_block(block_id)
                st.rerun()
            except Exception as exc:
                show_error(exc)
